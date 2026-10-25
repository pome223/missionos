"""Independent saved-record checks; no simulation, tracker or supervisor replay."""
from .starship_operational_trials import REGISTERED_CASES
from .starship_ascent_supervisor import CONFIG
from .starship_deployment_handoff_verifier import (
    STATE, _digest, _hash, _history, _number, _observed_origin, _require,
    _same, _state, _strict, _vehicle_inventory,
)
from .starship_m2_contract import CHECKPOINT_TIME_S, m2_contract_matches

SCOPE = "concurrent_launch_ascent_deployment_prefix_only"


def _authority(study):
    authority, binding, profile = (study[k] for k in ("authority", "approval_binding", "profile"))
    mode = authority["mode"]
    _require(mode in {"fixture", "live"}, "prefix_mode")
    _require(authority["case"] in {"normal", "operations_notice", "release_fault", "orbital_update", *REGISTERED_CASES}, "unregistered_prefix_case")
    _require(authority["schema"] in {"missionos.starship_m3_prefix_authority.v1", "missionos.starship_m3_prefix_authority.v2", "missionos.starship_m3_prefix_authority.v3", "missionos.starship_m3_prefix_authority.v4"}, "prefix_authority_version")
    splashdown = authority["schema"] != "missionos.starship_m3_prefix_authority.v1"
    _require((mode == "live") is (authority["schema"] in {"missionos.starship_m3_prefix_authority.v3", "missionos.starship_m3_prefix_authority.v4"})
, "prefix_live_scope")
    inner = authority["m2_authority"]
    _require(m2_contract_matches(inner) and inner["mode"] == mode
             and inner["case"] == authority["case"], "prefix_inner_authority_binding")
    expected = {"schema": "missionos.starship_m3_prefix_authority.v1", "scope": SCOPE,
        "mode": mode, "case": authority["case"], "m2_authority": inner,
        "m2_authority_sha256": _digest(inner), "ascent_authority": CONFIG,
        "ascent_authority_sha256": _digest(CONFIG), "profile_sha256": inner["profile_sha256"],
        "checkpoint_time_s": CHECKPOINT_TIME_S, "booster_control": "fixed_v1",
        "booster_coast_control": "fixed_v1", "maximum_model_calls": 0, "maximum_forecasts": 0,
        "ship_return_dispatch_authorized": False, "physical_execution_authorized": False,
        "subauthority_policy": "outer_prefix_scope_intersects_embedded_m2_grant"}
    if splashdown:
        from .starship_mission_director import contract
        booster = contract(mode, splashdown=True)
        expected.update(schema="missionos.starship_m3_prefix_authority.v2",
            booster_authority=booster, booster_authority_sha256=_digest(booster),
            director_routing="body_specific_grants_v1")
    if mode == "live":
        expected.update(schema="missionos.starship_m3_prefix_authority.v3",
            maximum_ascent_jev_calls=4, maximum_booster_jev_calls=1,
            prefix_provider_policy="jev_only",
            maximum_model_calls=inner["deployment_authority"]["maximum_jev_calls"]+5)
    if authority["schema"] == "missionos.starship_m3_prefix_authority.v4":
        report = authority["booster_recovery_report"]
        from .starship_recovery_reports import report_sources
        provenance = report_sources(report)
        _require(type(report) is dict and report.get("schema") == "missionos.booster_recovery_mission_report.v1"
            and report.get("grants_execution_authority") is False
            and report.get("physical_execution") is False
            and report["catch"]["capture_controller_connected"] is False,
            "recovery_report_scope")
        expected.update(schema=authority["schema"], maximum_booster_jev_calls=0,
            maximum_booster_llm_calls=None, prefix_provider_policy="jev_ascent_deepseek_booster_report_v1",
            booster_recovery_report=report, booster_recovery_report_sha256=_digest(report),
            booster_recovery_report_source_sha256=provenance, maximum_model_calls=None)
    _require(_same(authority, expected) and _digest(profile) == authority["profile_sha256"], "outer_authority_or_profile")
    expected_binding = {"schema": "missionos.starship_m3_prefix_approval_binding.v1",
        "mode": mode, "outer_authority_sha256": _digest(authority),
        "m2_authority_sha256": _digest(inner), "ascent_authority_sha256": _digest(CONFIG),
        "human_approval_authenticated": False}
    if splashdown:
        import re
        _require(type(study["run_id"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", study["run_id"]) is not None, "prefix_run_id")
        expected_binding["booster_authority_sha256"] = _digest(booster)
        expected_binding["run_id"] = study["run_id"]
    _require(_same(binding, expected_binding), "outer_approval_binding")
    run = study["prefix_run"]
    _require(run["scenario"] == "launch" and run["mission_management_case"] == inner["deployment_case"]
             and run["mission_director"]["response_fault"] is None
             and _same(run["mission_director"]["envelope"], inner["deployment_authority"]), "inner_deployment_authority")
    if splashdown:
        _require(all(r["request"]["request_id"] == study["run_id"] + f"-deployment:{i+1}"
                     for i, r in enumerate(run["mission_director"]["records"])), "ship_request_run_id_binding")
    _require(not any(e["event"] in {"return_requested", "deorbit_ignition"} for e in run["events"])
             and not any(r["request"]["point"] == "return_selection" for r in run["mission_director"]["records"]), "ship_return_outside_prefix")
    _require("development_coast_from_separation" not in run["booster_run"], "unapproved_booster_controller")
    if not splashdown:
        _require("booster_mission_director" not in run and "splashdown" not in run["booster_run"], "unapproved_splashdown_goal")


def _booster_splashdown(study, rows):
    """Recheck the separate grant, actual command, later sample and goal."""
    from .starship_mission_director import check_action, eligible_actions, fallback_action, validate_observation
    from .starship_sixdof_verifier import verify_study
    from .starship_splashdown_verifier import verify_splashdown

    run, envelope = study["prefix_run"], study["authority"]["booster_authority"]
    booster, record = run["booster_run"], run["booster_mission_director"]
    _require(record["schema"] == "missionos.starship_director_record.v1"
             and _same(record["envelope"], envelope) and record["response_fault"] is None
             and record["observation_requests"] == 0 and record["hold_used_s"] == 0
             and record["human_inflight_commands"] == 0 and record["human_workload_measured"] is False
             and record["model_value_demonstrated"] is False, "booster_director_scope")
    _require(not any(r["clock_id"] == "booster" or r["request"]["point"] == "booster_selection"
                     for r in run["mission_director"]["records"]), "booster_record_in_ship_grant")
    _require(len(record["records"]) == 1, "booster_decision_count")
    item = record["records"][0]
    request, response, dispatch = (item[k] for k in ("request", "response", "dispatch"))
    _require(item["clock_id"] == "booster" and request["point"] == "booster_selection"
             and request["request_id"] == study["run_id"] + "-booster:1"
             and request["schema"] == "missionos.starship_director_request.v5"
             and request["envelope_sha256"] == _digest(envelope), "booster_request_authority")
    before, now, later = (validate_observation(x) for x in (
        request["observation"], dispatch["observation"], item["later_observation"]))
    tick_times = {r["time_s"] for r in rows if r["body_id"] == "booster"}
    _require(all(r["phase"] == "booster_return" and r["time_s"] in tick_times
                 for r in (before, now, later))
             and before["time_s"] <= now["time_s"] == dispatch["time_s"] < later["time_s"]
             <= booster["final_state"]["time_s"], "booster_later_execution_tick")
    deadline = request["decision_deadline_s"]
    _require(_number(deadline) and before["time_s"] < deadline <= before["time_s"] + envelope["decision_expiry_s"]
             and _same(request["allowed_actions"], eligible_actions(envelope, "booster_selection", before, observation_requests=0, hold_used_s=0.)),
             "booster_request_choices_or_deadline")
    if study["authority"]["mode"] == "fixture":
        _require(response["model_inference_invoked"] is False
                 and all(response.get(k) is None for k in ("jev_invocation", "llm_invocation")), "booster_fixture_only")
    reason = check_action(envelope, "booster_selection", response.get("action"), now,
                          elapsed_s=now["time_s"]-before["time_s"], observation_requests=0, hold_used_s=0.)
    if now["time_s"] >= deadline:
        reason = "decision_deadline_reached"
    if response.get("action") not in request["allowed_actions"]:
        reason = reason or "outside_request_choices"
    if (response.get("request_id") != request["request_id"]
            or response.get("request_sha256") != _digest(request) or response.get("mode") != study["authority"]["mode"]):
        reason = reason or "response_binding_mismatch"
    fallback = fallback_action(envelope, "booster_selection", now, observation_requests=0, hold_used_s=0.)
    fallback_reason = check_action(envelope, "booster_selection", fallback, now,
        elapsed_s=0., observation_requests=0, hold_used_s=0.) if reason else None
    _require(dispatch["rejection"] == reason and dispatch["rules_accepted"] is (reason is None)
             and dispatch["fallback_rejection"] == fallback_reason
             and dispatch["fallback_rules_accepted"] is (reason is not None and fallback_reason is None)
             and dispatch["action"] == (fallback if reason else response["action"]) == "splashdown", "booster_dispatch_check")
    commands = [e for e in booster["events"] if e["event"] == "managed_booster_command"]
    _require(len(commands) == 1 and commands[0]["action"] == dispatch["action"]
             and commands[0]["time_s"] == dispatch["time_s"]
             and commands[0]["return_site_sha256"] == _digest(booster["active_return_site"])
             == envelope["splashdown_goal"]["reference_site_sha256"], "booster_command_site_binding")
    trajectory = verify_study({"schema": "missionos.starship_sixdof_study.v1", "profile": study["profile"],
        "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False},
        "runs": [run]}, expected_scenario="launch")
    _require(trajectory["passed"] is True, "booster_trajectory_record_invalid")
    water = verify_splashdown(booster, envelope["splashdown_goal"])
    _require(water["passed"] is True, "booster_splashdown_record_invalid")
    return {**water, "trajectory_record_verified": True, "independent_physics_validation": False}


def _clock(study):
    run, profile = study["prefix_run"], study["profile"]
    clock = run["concurrent_execution"]
    bound = max(profile["integration"]["powered_dt_s"], profile["integration"]["coast_dt_s"])
    _require(clock["schema"] == "missionos.ship_booster_clock.v1"
             and clock["posthoc_ordering"] is False and clock["physical_execution"] is False
             and clock["observers_run_before_each_body_step"] is True
             and clock["maximum_tick_s"] == bound
             and clock["absolute_deadline_s"] == CHECKPOINT_TIME_S, "shared_clock_contract")
    rows, bodies = clock["tick_order"], {}
    _require(type(rows) is list and bool(rows), "missing_execution_tick_order")
    for index, row in enumerate(rows):
        _require(type(row["sequence"]) is int and row["sequence"] == index
                 and row["body_id"] in {"ship", "booster"}
                 and _number(row["time_s"]) and _number(row["next_time_s"])
                 and 0 <= row["time_s"] <= row["next_time_s"] <= row["time_s"]+bound+1e-8,
                 "invalid_actual_tick")
        _require(index == 0 or row["time_s"] >= rows[index-1]["time_s"]-1e-9, "execution_order_reversed")
        bodies.setdefault(row["body_id"], []).append(row)
    _require(set(bodies) == {"ship", "booster"} and bodies["ship"][0]["time_s"] == 0., "both_physical_bodies_required")
    for body, own in bodies.items():
        final = run["final_state"] if body == "ship" else run["booster_run"]["final_state"]
        _state(final)
        _require(own[-1]["next_time_s"] == final["time_s"] <= CHECKPOINT_TIME_S+1e-8
                 and all(a["next_time_s"] == b["time_s"] for a, b in zip(own, own[1:])), "body_step_continuity")
    booster = run["booster_run"]
    _require(_same(booster["shared_clock_checkpoint"], {"absolute_deadline_s": CHECKPOINT_TIME_S,
        "unfinished": booster["outcome"]["termination"] == "time_limit", "state_reset": False}), "booster_checkpoint_claim")
    pending = {body: own[0]["time_s"] for body, own in bodies.items()}
    remaining = {body: len(own) for body, own in bodies.items()}
    lead = 0.
    for row in rows:
        body = row["body_id"]
        _require(row["time_s"] == min(pending.values()), "body_stepped_ahead_of_pending_clock")
        remaining[body] -= 1
        if remaining[body]:
            pending[body] = row["next_time_s"]
        else:
            del pending[body]
        if len(pending) == 2:
            # The booster first becomes pending at the separation, not launch.
            if row["time_s"] >= bodies["booster"][0]["time_s"]:
                lead = max(lead, max(pending.values())-min(pending.values()))
    _require(lead <= bound+1e-8 and abs(lead-clock["maximum_observed_body_lead_s"]) <= 1e-8, "shared_clock_lead_claim")
    separation = run["booster_separation_state"]
    _state(separation)
    events = [e for e in run["events"] if e["event"] == "stage_separation"]
    _require(len(events) == 1 and events[0]["time_s"] == separation["time_s"] == bodies["booster"][0]["time_s"]
             and _same(separation, run["booster_run"]["booster_separation_state"])
             and clock["separation_state_preserved"] is True, "exact_separation_identity")
    return rows


def _observation_tick(observed, rows):
    keys = ("time_s", "phase", "sequencer_state", "released_count", "deployment_withheld")
    matches = [r for r in rows if r["body_id"] == "ship" and all(_same(r[k], observed[k]) for k in keys)]
    _require(bool(matches), "ascent_observation_not_in_execution_ticks")


def _ascent(study, rows):
    record, run = study["ascent"], study["prefix_run"]
    _require(record["schema"] == "missionos.starship_ascent_supervision.v1"
             and _same(record["authority"], CONFIG) and record["mode"] == study["authority"]["mode"]
             and record["physical_execution"] is False and record["abort_trajectory_implemented"] is False,
             "ascent_scope")
    witnesses = {}
    for witness in study["ascent_witnesses"]:
        i, state, observed = witness["sequence"], witness["state"], witness["observation"]
        _require(type(i) is int and 0 <= i < len(rows), "ascent_witness_tick")
        _state(state)
        _observation_tick(observed, [rows[i]])
        _require(state["time_s"] == observed["time_s"]
                 and _same(observed["navigation"], _observed_origin(state, 0))
                 and _same(observed["engine_available"], [e["available"] for e in state["engine_states"]]),
                 "ascent_sensor_witness")
        witnesses[_digest(observed)] = witness
    by_time = {}
    for row in rows:
        if row["body_id"] == "ship":
            by_time.setdefault(row["time_s"], []).append(row)
    for observed in record["observations"]:
        _observation_tick(observed, by_time.get(observed["time_s"], []))
    decisions = record["decisions"]
    triggers = [t for t in record["trigger_events"] if t.get("model_request") is not False]
    interlocks = [t for t in record["trigger_events"] if t.get("model_request") is False]
    _require(bool(record["observations"]) and record["observations"][0]["time_s"] == 0.
             and bool(record["trigger_events"]) and record["pending"] is None, "ascent_monitoring_missing_or_pending")
    _require(type(record["requests"]) is int and record["requests"] == len(triggers) <= CONFIG["maximum_requests"]
             and len(decisions)+(record["pending"] is not None) == len(triggers), "ascent_request_accounting")
    withheld_at = None
    for event in interlocks:
        _require(set(event) == {"time_s", "trigger", "model_request", "action"}
                 and event["trigger"] == "observed_engine_loss_interlock" and event["action"] == "withhold_deployment"
                 and any(w["observation"]["time_s"] == event["time_s"] and not all(w["observation"]["engine_available"])
                         for w in witnesses.values()), "unbacked_health_interlock")
        _require(withheld_at is None, "duplicate_health_interlock")
        withheld_at = event["time_s"]
    for index, (entry, trigger) in enumerate(zip(decisions, triggers)):
        if study["authority"]["mode"] == "live":
            _require(entry["request"]["request_id"] == f"{study['run_id']}-ascent-{index}", "ascent_run_id_binding")
        request, observed = entry["request"], entry["observation"]
        issued, now = request["observation"]["time_s"], entry["time_s"]
        _require(_digest(observed) in witnesses and _digest(request["observation"]) in witnesses,
                 "decision_needs_actual_sensor_witness")
        _require(request["schema"] == "missionos.starship_ascent_decision.v1"
                 and request["authority_sha256"] == _digest(CONFIG)
                 and _same(request["allowed_actions"], CONFIG["actions"])
                 and request["deadline_s"] == issued+CONFIG["decision_timeout_s"]
                 and trigger["time_s"] == issued and trigger["request_id"] == request["request_id"]
                 and trigger["trigger"] == request["trigger"] and issued < now == observed["time_s"]
                 and entry["physical_engine_command"] is False, "ascent_decision_binding")
        response = entry["response"]
        same_stage = observed["phase"] == request["observation"]["phase"]
        stale = same_stage and not _same(observed["engine_available"], request["observation"]["engine_available"])
        unsafe_continue = (response if type(response) is dict else {}).get("action") == "continue_mission" and (stale or not all(observed["engine_available"]))
        valid = (type(response) is dict and response.get("request_sha256") == _digest(request)
                 and response.get("request_id") == request["request_id"]
                 and response.get("action") in CONFIG["actions"] and now < request["deadline_s"] and not unsafe_continue
                 and (study["authority"]["mode"] == "fixture" or _successful_live_response(response)))
        action = response["action"] if valid else "continue_mission" if all(observed["engine_available"]) else "withhold_deployment"
        _require(entry["stale_health"] is stale and entry["fallback"] is (not valid)
                 and entry["action"] == action and entry["accepted_action"] == (action if valid else None)
                 and (study["authority"]["mode"] == "live" or response is None
                      or response.get("model_inference_invoked") is False), "ascent_action_or_fallback")
        if study["authority"]["mode"] == "live":
            later = entry["later_observation"]
            _require(type(later) is dict and _digest(later) in witnesses
                     and later["time_s"] > now, "ascent_live_later_observation_missing")
        if action == "withhold_deployment":
            withheld_at = min(now, withheld_at) if withheld_at is not None else now
    events = [e for e in run["events"] if e["event"] == "supervised_deployment_withheld"]
    effect = run["deployment_permission"]
    _require(record["withheld"] is (withheld_at is not None) and effect["withheld"] is record["withheld"], "ascent_plant_latch_disagreement")
    if withheld_at is None:
        _require(not events and not record["confirmations"]
                 and not any(r.get("deployment_withheld") for r in rows), "uncommanded_withhold")
    else:
        _require(len(events) == 1 and events[0]["time_s"] == withheld_at
                 and effect["sequencer_state"] == "skipped"
                 and not any(e["event"] == "payload_released" and e["time_s"] >= withheld_at for e in run["events"]), "withhold_actual_sequencer_effect")
        confirmations = record["confirmations"]
        _require(len(confirmations) == 1 and confirmations[0]["command_time_s"] == withheld_at, "withhold_later_confirmation_missing")
        observed = confirmations[0]["later_observation"]
        _require(_digest(observed) in witnesses and observed["time_s"] > withheld_at
                 and observed["deployment_withheld"] is True and observed["sequencer_state"] == "skipped", "withhold_not_later_observed")
        _require(all(r["deployment_withheld"] is True for r in rows if r["body_id"] == "ship" and r["time_s"] > withheld_at), "withhold_latch_cleared")
    _require(effect["payload_released_count"] == run["outcome"]["payload_released_count"]
             and effect["observed_time_s"] == run["final_state"]["time_s"], "final_sequencer_receipt")


def _handoff(study):
    handoff, run, profile, trace = (study[k] for k in ("handoff", "prefix_run", "profile", "trace"))
    if "unified_composition" in study:
        from .starship_unified_contract import validate_composition
        from .starship_m2_contract import validate_deployment_origin
        from .starship_visual_verifier import validate_visual_origin
        comp = validate_composition(study["unified_composition"])
        _require(study["authority"] == comp["authority"]["prefix_authority"], "unified_prefix_authority")
        _require(run["visual_observation"]["authority"] == comp["visual_authority"], "unified_prefix_visual_binding")
        from .starship_trouble_engine import manifest_digest
        _require(run["trouble"]["manifest_sha256"] == manifest_digest(comp["prefix_faults"]), "unified_prefix_fault_binding")
        if handoff is None:
            _require(type(study["handoff_error"]) is str and bool(study["handoff_error"]), "missing_handoff_reason")
            return False
        _require(study["handoff_error"] is None, "unified_handoff_error")
        validate_deployment_origin(profile, comp["authority"]["m2_authority"], handoff, run,
                                   visual_authority=comp["visual_authority"])
        validate_visual_origin(comp["visual_authority"], comp["authority"]["m2_authority"], handoff, run, trace=trace)
        return True
    if handoff is None:
        _require(type(study["handoff_error"]) is str and bool(study["handoff_error"]), "missing_handoff_reason")
        return False
    _require(study["handoff_error"] is None and handoff["schema"] == "missionos.deployment_return_handoff.v1"
             and handoff["development_only"] is True and handoff["dispatch_authorized"] is False
             and handoff["inventory_basis"] == "synthetic_confirmed_separation_counter_v1"
             and handoff["authority_sha256"] == study["authority"]["m2_authority_sha256"]
             and handoff["profile_sha256"] == _digest(profile)
             and handoff["handoff_sha256"] == _digest({k: v for k, v in handoff.items() if k != "handoff_sha256"}), "handoff_authority_binding")
    total = profile["payload"]["count"]
    history, releases, latest, generation = _history(trace, total)
    _require(_same(handoff["history"], history) and _same(handoff["release_observations"], releases)
             and type(handoff["generation"]) is int and handoff["generation"] == generation, "handoff_observed_history")
    released = latest["released_count"]
    _require(_same([handoff["total_count"], handoff["released_count"], handoff["retained_count"]], [total, released, total-released])
             and run["outcome"]["payload_released_count"] == released, "handoff_inventory")
    final = run["final_state"]
    _require(_same(handoff["execution_state"], final) and run["outcome"]["phase"] == "orbital_coast"
             and abs(final["time_s"]-CHECKPOINT_TIME_S) <= 1e-8
             and 0 <= final["time_s"]-latest["time_s"] <= 2.001
             and all(_same(run["samples"][-1][k], final[k]) for k in STATE)
             and _same(handoff["origin"], _observed_origin(final, total-released)), "exact_continuation_state")
    _vehicle_inventory(profile, run["final_vehicle"], total-released)
    events = [e for e in run["events"] if e["event"] == "payload_released"]
    _require(len(events) == len(releases) == len(run["satellites"]) == released, "physical_release_inventory")
    for event, obs, satellite in zip(events, releases, run["satellites"]):
        _require(obs["previous_observation_time_s"] <= event["time_s"] <= obs["observed_time_s"]
                 and event["time_s"] == satellite["release_state"]["time_s"], "release_event_timeframe")
    _deployment_sequences(run, trace)
    return True


def _deployment_sequences(run, trace):
    observations = [e["row"] for e in trace if e["kind"] == "observation"]
    relevant = [e for e in run["events"] if e["event"] in {"supervised_deployment_withheld", "managed_hold_expired", "deployment_interlock_inhibited"}
                or e["event"] == "managed_mission_command" and e["point"].startswith("deployment")]
    timeline = [(e["time_s"], 0 if e["event"] in {"supervised_deployment_withheld", "managed_hold_expired"} else 2, i, e) for i, e in enumerate(relevant)]
    timeline += [(e["time_s"], 1, i, e) for i, e in enumerate(observations)]
    sequence, latched = "running", False
    for _, priority, _, row in sorted(timeline):
        if priority == 1:
            _require(row["sequencer_state"] == ("skipped" if latched else sequence), "deployment_sequence_not_in_plant")
        elif row["event"] == "supervised_deployment_withheld":
            latched, sequence = True, "skipped"
        elif row["event"] == "managed_hold_expired":
            _require(sequence == "held", "expiry_without_hold")
            sequence = "skipped"
        elif row["event"] == "deployment_interlock_inhibited":
            sequence = "inhibited"
        else:
            action = row["action"]
            if action in {"hold", "stop_deployment"}:
                sequence = "held" if action == "hold" else "skipped"
            elif action == "collect_status" and sequence not in {"skipped", "inhibited"}:
                sequence = "held"
            elif action == "continue" and sequence == "held":
                sequence = "running"
    commands = [e for e in relevant if e["event"] == "managed_mission_command"]
    _require(_same([{k: e[k] for k in ("action", "time_s")} for e in commands],
                  [{k: e[k] for k in ("action", "time_s")} for e in trace if e["kind"] == "command"]), "deployment_command_trace")
    observed, dispatched = {_digest(o) for o in observations}, []
    for record in run["mission_director"]["records"]:
        if record["clock_id"] != "ship":
            continue
        request, dispatch, later = record["request"], record["dispatch"], record["later_observation"]
        _require(dispatch is not None and later is not None
                 and _digest(request["observation"]) in observed and _digest(dispatch["observation"]) in observed
                 and _digest(later) in observed and request["observation"]["time_s"] <= dispatch["time_s"] < later["time_s"], "deployment_later_evidence")
        dispatched.append({"point": request["point"], "action": dispatch["action"], "time_s": dispatch["time_s"]})
    _require(_same(dispatched, [{k: e[k] for k in ("point", "action", "time_s")} for e in commands]), "deployment_core_dispatch")


def _successful_live_response(response, provider="typesafe"):
    key, other = ("llm_invocation", "jev_invocation") if provider == "deepseek" else ("jev_invocation", "llm_invocation")
    receipt = response.get(key)
    return (response.get("mode") == "live" and response.get("model_inference_invoked") is True
            and response.get(other) is None and type(receipt) is dict
            and receipt.get("call_attempted") is True and receipt.get("model_inference_invoked") is True
            and receipt.get("provider") == provider and receipt.get("status") == "succeeded")


def _providers(study):
    """Independently bind actual short-phase calls to the host's durable ledger.

    Runtime receipts establish provider invocation; checks here establish record
    consistency, not independent authentication of a provider's remote execution.
    """
    from .starship_m2_attempts import FIELDS, empty
    from .starship_m2_decision_verifier import verify_decisions
    authority, run_id = study["authority"], study.get("run_id")
    if authority["mode"] == "fixture":
        _require(study["model_calls"] == 0 and not study.get("provider_attempt_ledger")
                 and not study.get("deployment_attempt_ledger"), "fixture_provider_claim")
        return {"attempted": 0, "actor_valid_responses": 0, "rules_accepted": 0}
    ledgers = study["provider_attempt_ledger"]
    _require(type(ledgers) is dict and set(ledgers) == {"ascent", "booster"}, "prefix_host_ledgers_required")
    summary = {"attempted": 0, "actor_valid_responses": 0, "rules_accepted": 0}
    for phase in ("ascent", "booster"):
        deep = phase == "booster" and authority["schema"] == "missionos.starship_m3_prefix_authority.v4"
        wanted = "deepseek" if deep else "typesafe"
        key, other = ("llm_invocation", "jev_invocation") if deep else ("jev_invocation", "llm_invocation")
        records = (study["ascent"]["decisions"] if phase == "ascent"
                   else study["prefix_run"]["booster_mission_director"]["records"])
        canonical = {"schema": "missionos.starship_m3_host_attempts.v1",
            "authority_sha256": _digest(authority), "run_id": run_id, "phase": phase,
            "clock_mapping": "request_sim_seconds_as_wall_budget_v1",
            "adoption_deadline": "actor_rechecks_current_simulation_time", "attempts": []}
        ledger = ledgers[phase]
        _require(type(ledger) is dict and set(ledger) == set(canonical)
                 and all(_same(ledger[k], v) for k, v in canonical.items() if k != "attempts")
                 and type(ledger["attempts"]) is list
                 and len(ledger["attempts"]) <= (len(records) if deep else authority[f"maximum_{phase}_jev_calls"]), "prefix_host_ledger_binding")
        known, seen, matched = {r["request"]["request_id"]: r for r in records}, set(), set()
        for number, attempt in enumerate(ledger["attempts"], 1):
            _require(type(attempt) is dict and set(attempt) == FIELDS, "prefix_host_attempt_schema")
            token, provider = attempt["request_id"], attempt["provider"]
            _require(token in known and token not in seen and provider == wanted
                     and type(attempt["sequence"]) is int and attempt["sequence"] == number,
                     "prefix_host_attempt_identity")
            seen.add(token)
            entry, request = known[token], known[token]["request"]
            deadline = request["deadline_s" if phase == "ascent" else "decision_deadline_s"]
            budget = deadline-request["observation"]["time_s"]
            age, remaining, timeout = (attempt[k] for k in ("request_file_age_s", "remaining_s", "timeout_s"))
            _require(attempt["request_sha256"] == _digest(request) and _hash(attempt["provider_request_sha256"])
                     and all(_number(v) for v in (age, remaining, timeout)) and age >= 0
                     and 5 <= remaining <= budget-age+1e-6 and 3 <= timeout <= min(30., remaining-2.),
                     "prefix_host_attempt_deadline_or_binding")
            _require(all(type(attempt[k]) is bool for k in ("completed", "valid_response", "response_published"))
                     and attempt["status"] in {"in_progress", "succeeded", "transport_failed", "cancelled_request", "transport_timeout", "response_too_large", "invalid_response", "response_after_deadline"}
                     and (attempt["status"] == "in_progress") is (not attempt["completed"])
                     and attempt["valid_response"] is (attempt["status"] == "succeeded")
                     and (not attempt["response_published"] or attempt["completed"]), "prefix_host_attempt_status")
            response = entry.get("response")
            _require(response is None or type(response) is dict, "prefix_response_object")
            receipt = (response or {}).get(key)
            _require(receipt is None or type(receipt) is dict, "prefix_receipt_object")
            if receipt and receipt.get("call_attempted"):
                _require(attempt["completed"] and attempt["response_published"]
                         and receipt.get("request_sha256") == attempt["provider_request_sha256"]
                         and receipt.get("status") == attempt["status"]
                         and receipt.get("timeout_seconds") == timeout, "prefix_host_actor_mismatch")
                matched.add(token)
            summary["attempted"] += 1
        for entry in records:
            response = entry.get("response") or {}
            _require(type(response) is dict, "prefix_response_object")
            _require(response.get(other) is None, "short_phase_approved_provider_only")
            if deep and response:
                request = entry["request"]
                context = response.get("decision_context")
                expected_context = {"scope": "bounded_booster_supervision",
                    "observation": request["observation"], "allowed_actions": request["allowed_actions"],
                    "decision_deadline_s": request["decision_deadline_s"],
                    "recovery_report": authority["booster_recovery_report"],
                    "recovery_report_sha256": authority["booster_recovery_report_sha256"],
                    "objectives_to_compare": ["capture", "splashdown"],
                    "capture_controller_connected": authority["booster_authority"]["capture_controller_connected"]}
                _require(_same(context, expected_context)
                    and response.get("recovery_report_sha256") == authority["booster_recovery_report_sha256"]
                    and response.get("free_text_summary_verified") is False,
                    "booster_model_report_or_context_binding")
            receipt = response.get(key)
            invoked = False
            if receipt is not None:
                _require(type(receipt) is dict and type(receipt.get("call_attempted")) is bool,
                         "prefix_provider_receipt")
                if receipt["call_attempted"]:
                    _require(entry["request"]["request_id"] in matched
                             and receipt.get("schema_version") == "runtime_invocation_evidence.v1"
                             and receipt.get("provider") == wanted and receipt.get("mode") == "live"
                             and receipt.get("application_margin_s") == 2. and receipt.get("retries") == 0
                             and type(receipt.get("model_inference_invoked")) is bool
                             and receipt["model_inference_invoked"] is (receipt.get("status") == "succeeded"),
                             "prefix_actor_receipt_without_attempt")
                    invoked = receipt["model_inference_invoked"]
            if deep and receipt:
                _require(receipt.get("public_input_sha256") == _digest(response["decision_context"])
                    and receipt.get("maximum_instance_calls") is None,
                    "booster_approved_report_input_receipt")
                if invoked:
                    _require(response.get("action") in {"capture", "splashdown"}
                        and type(response.get("decision_summary")) is str
                        and 1 <= len(response["decision_summary"]) <= 1600, "booster_external_summary")
            _require(response.get("model_inference_invoked", False) is invoked, "prefix_inference_claim")
            accepted = (entry["accepted_action"] is not None if phase == "ascent"
                        else entry["dispatch"]["rules_accepted"])
            _require(not accepted or _successful_live_response(response, wanted), "accepted_short_phase_requires_live_receipt")
            summary["actor_valid_responses"] += invoked
            summary["rules_accepted"] += accepted
    inner, deployment = authority["m2_authority"], study["deployment_attempt_ledger"]
    receipt_count = sum(((r.get("response") or {}).get("jev_invocation") or {}).get("call_attempted") is True
                        for r in study["prefix_run"]["mission_director"]["records"])
    # Reuse the independent M2 record verifier for the unchanged deployment
    # subauthority; this does not manufacture a return operation or claim.
    deployed = verify_decisions({"authority": inner, "run_id": run_id,
        "deployment_run": study["prefix_run"], "return_study": {"decisions": []},
        "runtime": {"actor_record_model_calls": receipt_count,
                    "model_calls": len(deployment["attempts"])},
        "provider_attempt_ledger": {"deployment": deployment, "return": empty(inner, run_id, "return")}})
    summary["deployment"] = deployed
    from .starship_call_limits import call_count_allowed
    _require(study["model_calls"] == summary["attempted"]+len(deployment["attempts"])
             and call_count_allowed(study["model_calls"], authority["maximum_model_calls"]), "prefix_total_model_budget")
    return summary


def verify(study):
    result = {"schema": "missionos.starship_m3_prefix_verification.v1", "scope": SCOPE,
        "passed": False, "issues": [], "handoff_ready": False, "ship_return_verified": False,
        "booster_recovery_verified": False, "m3_completed": False, "physical_execution": False,
        "independent_physics_validation": False, "source_manifest_validated_against_external_commit": False}
    try:
        _require(type(study) is dict and _strict(study) and study["schema"] == "missionos.starship_m3_prefix_study.v1"
                 and study["scope"] == SCOPE and type(study["model_calls"]) is int and study["model_calls"] >= 0
                 and type(study["forecasts"]) is int and study["forecasts"] == 0
                 and all(study[k] is False for k in ("ship_return_dispatched", "mission_completed", "m3_completed", "physical_execution")), "prefix_claim_scope")
        _require(bool(study["source_sha256"]) and all(type(p) is str and not p.startswith(("/", "../")) and _hash(h)
                    for p, h in study["source_sha256"].items()), "portable_source_manifest")
        _authority(study)
        rows = _clock(study)
        _ascent(study, rows)
        if study["authority"]["schema"] != "missionos.starship_m3_prefix_authority.v1":
            result["booster_splashdown"] = _booster_splashdown(study, rows)
        result["provider_accounting"] = _providers(study)
        result["handoff_ready"] = _handoff(study)
        result["passed"] = True
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, ZeroDivisionError, RecursionError) as error:
        result["issues"].append(str(error) if type(error) is ValueError else f"malformed_record:{type(error).__name__}")
    return result

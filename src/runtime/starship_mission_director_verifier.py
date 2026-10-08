"""Read-only checks of mission decisions against saved events and trajectories.

Stored-record validity, physical mission success and comparative acceptance are
three different results. This verifier never asks a model to grade its answer.
"""
from __future__ import annotations

import math
from hashlib import sha256
from pathlib import Path

from .starship_mission_director import RESPONSE_FAULTS, COMPOUND_FAULTS, canonical_contract, digest, check_action, eligible_actions, fallback_action, validate_observation
from .starship_sixdof_verifier import verify_study
from .starship_retained_return_verifier import verify_retained_return


def metrics(run):
    contact = run["outcome"].get("contact_receipt")
    gate = False
    if contact:
        q = contact["q_body_to_eci"]
        axis = [2*(q[1]*q[3]+q[0]*q[2]), 2*(q[2]*q[3]-q[0]*q[1]), 1-2*(q[1]*q[1]+q[2]*q[2])]
        tilt = math.degrees(math.acos(max(-1., min(1., sum(a*b for a, b in zip(axis, contact["surface_normal_eci"]))))))
        rate = math.sqrt(sum(x*x for x in run["final_state"]["omega_body_rad_s"]))
        gate = (contact["surface_relative_speed_mps"] <= 5. and tilt <= 5. and rate <= .02
                and contact["propellant_kg"] >= 28000. and not run["outcome"].get("terminal_qualification_violated", False))
    result = {"released": run["outcome"]["payload_released_count"],
            "orbit": run["outcome"]["orbit_gate_reached"], "termination": run["outcome"]["termination"],
            "contact_speed_mps": contact["surface_relative_speed_mps"] if contact else None,
            "remaining_fuel_kg": run["final_state"]["propellant_kg"],
            "return_time_s": next((e["time_s"] for e in run["events"] if e["event"] == "return_requested"), None),
            "booster_destination": run["booster_run"].get("active_return_site", {}).get("site_id"),
            "booster_destination_reached": run["booster_run"].get("divert_destination_reached", False)}
    result["ship_return_status"] = ("qualified_modeled_contact" if gate else "contact_outside_qualification" if contact
                                    else "unresolved")
    if "splashdown" in run["booster_run"]:
        result["controlled_water_entry_envelope_met"] = run["booster_run"]["splashdown"]["controlled_water_entry_envelope_met"]
    result["booster_goal"] = "controlled_water_entry" if "splashdown" in run["booster_run"] else "diversion"
    return result


def comparison(case, baseline, managed):
    a, b = metrics(baseline), metrics(managed)
    same = (a == b)
    # Frozen before running: compare actual outcomes, not whether the provider
    # happened to be called. Deployment count alone is not success after an
    # explicit suspension notice. Preserve fuel and contact tradeoffs as data.
    if case == "normal":
        accepted = same and b["ship_return_status"] == "qualified_modeled_contact" and all(r["dispatch"]["action"] in ("continue", "state_return", "divert", "splashdown")
                               for r in managed["mission_director"]["records"] if r["dispatch"])
    elif case == "fuel_shortage":
        accepted = (a["orbit"] and b["orbit"] and b["released"] == 0
            and b["termination"] == "return_inhibited_unresolved" and b["contact_speed_mps"] is None
            and b["remaining_fuel_kg"] >= 1000.)
    else:
        speed_a, speed_b = a["contact_speed_mps"], b["contact_speed_mps"]
        accepted = (a["orbit"] == b["orbit"] and a["return_time_s"] == b["return_time_s"]
                    and b["ship_return_status"] == "qualified_modeled_contact"
                    and b["booster_destination"] == "divert"
                    and speed_a is not None and speed_b is not None and speed_b <= speed_a+.01)
        if case == "operations_notice":
            accepted = accepted and b["released"] <= 1
        else:
            accepted = accepted and b["released"] >= a["released"]
    return {"baseline": a, "managed": b, "normal_outcomes_equal": same if case == "normal" else None,
            "comparison_accepted": bool(accepted), "comparison_is_model_value_proof": False,
            "comparison_scope": "inhibit_and_observe_30s_only" if case == "fuel_shortage" else "scenario_regression",
            "ship_return_qualified": b["ship_return_status"] == "qualified_modeled_contact",
            "whole_mission_success_claimed": False,
            "human_workload_measured": False, "physical_success_claimed": False}


def _bind_return_observation(run, profile, row):
    """Bind the margin inputs to a separately recorded integrated state."""
    proof = row["numerical_tools"].get("return_feasibility")
    if type(proof) is not dict or type(proof.get("inputs")) is not dict:
        raise ValueError("return_state_missing")
    inputs = proof["inputs"]
    witnesses = [s for s in run["samples"] if s.get("return_execution_observation") == row
                 and s["time_s"] == row["time_s"]]
    if len(witnesses) != 1:
        raise ValueError("return_state_not_bound_to_trajectory")
    state = witnesses[0]
    for name in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s"):
        if inputs.get(name) != state.get(name):
            raise ValueError("return_navigation_does_not_match_observed_state")
    if abs(row["fuel_kg"]-state["propellant_kg"]) > 100.+1e-6:
        raise ValueError("return_fuel_sensor_outside_declared_uncertainty")
    p, a = profile["ship"], profile["actuators"]
    expected = {"available_landing_engines": sum(e["available"] for e in state["engine_states"][:3]),
        "dry_and_payload_mass_kg": p["dry_mass_kg"]+(26-row["released_count"])*profile["payload"]["mass_each_kg"],
        "engine_isp_s": p["engine_isp_s"], "deorbit_perigee_m": profile["guidance"]["deorbit_perigee_m"],
        "rcs_thrust_n": a["rcs_thrust_n"], "rcs_isp_s": a["rcs_isp_s"]}
    if any(inputs.get(k) != v for k, v in expected.items()):
        raise ValueError("return_model_parameter_mismatch")


def _return_execution_checks(run, profile, expected_envelope):
    from .starship_return_feasibility_verifier import verify as verify_margin, load_certificate, registered
    checks = run.get("return_execution_checks", [])
    for item in checks:
        row = validate_observation(item["observation"])
        _bind_return_observation(run, profile, row)
        if item["time_s"] != row["time_s"] or item["feasibility"] != row["numerical_tools"]["return_feasibility"]:
            raise ValueError("return_execution_not_fresh")
        certificate, identity = load_certificate()
        if not registered(profile, identity):
            certificate = None
        result = verify_margin(item["feasibility"], certificate, identity, time_s=row["time_s"],
            fuel_kg=row["fuel_kg"], released_count=row["released_count"])
        if not result["passed"]:
            raise ValueError("return_execution_margin_not_verified")
        if item["action"] == "state_return" and not item["feasibility"]["return_admitted"]:
            raise ValueError("unchecked_return_execution")
        if item["action"] == "inhibit_return" and not item["feasibility"]["bounded_coast_admitted"]:
            raise ValueError("unchecked_coast_execution")
    if run["retained_return"]["policy_id"] == "trimmed_state_terminal_v4":
        if len(checks) != 1 or checks[0]["action"] != "state_return":
            raise ValueError("missing_execution_time_return_check")
        terminal = run["retained_return"].get("terminal_feasibility")
        certificate, _ = load_certificate()
        state = run["retained_return"]["trigger"]["state"]
        budget = run["retained_return"]["trigger"]["budget"]
        expected_pass = (state["propellant_kg"]-100. >= certificate["maximum_terminal_consumption_kg"]+29000.
                         and budget["available_landing_engines"] == 3 and budget["aligned_net_acceleration_mps2"] > 0)
        if (type(terminal) is not dict or terminal.get("passed") is not expected_pass
                or abs(terminal["required_fuel_kg"]-certificate["maximum_terminal_consumption_kg"]-29000.) > 1e-6
                or abs(terminal["fuel_lower_bound_kg"]-(state["propellant_kg"]-100.)) > 1e-6
                or terminal.get("time_s") != state["time_s"]
                or terminal.get("available_landing_engines") != budget["available_landing_engines"]
                or terminal.get("aligned_net_acceleration_mps2") != budget["aligned_net_acceleration_mps2"]
                or terminal.get("continuation") != ("qualified_terminal_guidance" if expected_pass else "unqualified_best_effort_terminal_guidance")):
            raise ValueError("terminal_fuel_margin_not_verified")
        violations = [e for e in run["events"] if e["event"] == "terminal_return_domain_violation"]
        if (run["outcome"].get("terminal_qualification_violated") is not (not expected_pass)
                or len(violations) != int(not expected_pass)
                or violations and (violations[0].get("terminal_feasibility") != terminal
                                   or violations[0]["time_s"] != state["time_s"])
                or run["final_state"]["time_s"] <= state["time_s"]):
            raise ValueError("terminal_violation_outcome_not_preserved")


def _sequence_effects(run, records, envelope, profile):
    """Check the entire event intervals, not just observation snapshots."""
    releases = [e["time_s"] for e in run["events"] if e["event"] == "payload_released"]
    commands = [r["dispatch"] for r in records if r["clock_id"] == "ship" and r["dispatch"]]
    inhibited = [e["time_s"] for e in run["events"] if e["event"] == "deployment_interlock_inhibited"]
    if inhibited and any(t >= min(inhibited) for t in releases):
        raise ValueError("release_after_inhibit")
    if any(b["time_s"] <= a["time_s"] for a, b in zip(commands, commands[1:])):
        raise ValueError("ship_command_clock_regressed")
    for index, command in enumerate(commands):
        time, action = command["time_s"], command["action"]
        if action == "continue" and inhibited and time > min(inhibited):
            raise ValueError("inhibit_resume_forbidden")
        later = next(r["later_observation"] for r in records if r["dispatch"] is command)
        if action == "stop_deployment" and any(t >= time for t in releases):
            raise ValueError("release_after_stop")
        if action == "continue" and command["observation"]["sequencer_state"] == "held":
            resumed = [e for e in run["events"] if e["event"] == "managed_deployment_resumed" and e["time_s"] == time]
            newly_inhibited = any(e["event"] == "deployment_interlock_inhibited" and time <= e["time_s"] < later["time_s"] for e in run["events"])
            if len(resumed) != 1 or (later["sequencer_state"] != "running" and not (later["sequencer_state"] == "inhibited" and newly_inhibited)):
                raise ValueError("resume_effect_not_observed")
        if action not in ("hold", "collect_status"):
            continue
        successors = [c for c in commands[index+1:] if c["action"] in ("continue", "stop_deployment")]
        end = successors[0]["time_s"] if successors else run["final_state"]["time_s"]
        expires = time+envelope["maximum_hold_s"] if action == "hold" else command["observation"]["hold_expires_at_s"]
        if expires is not None:
            end = min(end, expires)
        if any(time <= t < end for t in releases):
            raise ValueError("release_during_hold_or_diagnostic")
        if action == "hold" and (not successors or successors[0]["time_s"] >= expires) and run["final_state"]["time_s"] >= expires:
            expired = [e for e in run["events"] if e["event"] == "managed_hold_expired" and expires <= e["time_s"] <= expires+profile["integration"]["coast_dt_s"]+1e-7]
            if len(expired) != 1:
                raise ValueError("hold_expiry_event_missing")
            if any(t >= expires for t in releases):
                raise ValueError("release_after_hold_expiry")


def verify(study, *, expected_case, expected_envelope, expected_run_id=None, expected_response_fault=None):
    issues, physical, observed_run_id = [], [], None
    try:
        if (study.get("schema") != "missionos.starship_managed_study.v1" or study.get("case") != expected_case
                or study.get("envelope") != expected_envelope or expected_envelope != canonical_contract(expected_envelope)
                or study.get("physical_execution") is not False or len(study["runs"]) != 2):
            raise ValueError("managed_input_binding")
        baseline, managed = study["runs"]
        for run in study["runs"]:
            result = verify_study({"schema": "missionos.starship_sixdof_study.v1", "profile": study["profile"],
                                   "provenance": {"physical_execution_invoked": False, "starship_vehicle_validated": False}, "runs": [run]}, expected_scenario="launch")
            physical.append(result)
            if not result["passed"]:
                issues.append({"code": "trajectory_record_invalid", "details": result["issues"]})
            if run["mission_management_case"] != expected_case:
                raise ValueError("case_mismatch")
            if run["booster_run"]["active_return_site"] != study["return_sites"]["sites"][1]:
                raise ValueError("unapproved_return_destination")
            if "splashdown_goal" in expected_envelope:
                from .starship_splashdown_verifier import verify_splashdown
                water = verify_splashdown(run["booster_run"], expected_envelope["splashdown_goal"])
                if not water["passed"]:
                    issues.append({"code":"splashdown_record_invalid","details":water["issues"]})
            elif "splashdown" in run["booster_run"]:
                raise ValueError("unapproved_splashdown_goal")
        if baseline["initial_state"] != managed["initial_state"]:
            raise ValueError("different_start_state")
        return_check = verify_retained_return(managed, study["profile"],
            expected_policy=managed["retained_return"]["policy_id"], expected_management_case=expected_case)
        if not return_check["passed"]:
            issues.append({"code": "return_record_invalid", "details": return_check["issues"]})
        record = managed["mission_director"]
        if (study.get("response_fault") != expected_response_fault or record.get("response_fault") != expected_response_fault
                or expected_response_fault is not None and (expected_envelope["mode"] != "fixture" or expected_response_fault not in RESPONSE_FAULTS)):
            raise ValueError("response_fault_binding")
        if record["envelope"] != expected_envelope or len(record["records"]) > expected_envelope["maximum_decisions"]:
            raise ValueError("envelope_or_decision_budget")
        seen, points, reads, hold = set(), set(), 0, 0.
        hold_dispatch = None
        for index, item in enumerate(record["records"]):
            request, dispatch, response = item["request"], item["dispatch"], item["response"]
            if request["request_id"] in seen or request["envelope_sha256"] != digest(expected_envelope):
                raise ValueError("request_binding")
            if expected_run_id and not request["request_id"].startswith(expected_run_id+":"):
                raise ValueError("cross_run_response")
            seen.add(request["request_id"])
            run_id = request["request_id"].rsplit(":", 1)[0]
            if observed_run_id is None:
                observed_run_id = run_id
            if run_id != observed_run_id or expected_run_id is not None and run_id != expected_run_id:
                raise ValueError("cross_run_response")
            if request["point"] in points:
                raise ValueError("duplicate_decision_point")
            points.add(request["point"])
            validate_observation(request["observation"])
            if request["allowed_actions"] != eligible_actions(expected_envelope, request["point"], request["observation"],
                    observation_requests=reads, hold_used_s=hold):
                raise ValueError("request_choice_budget_mismatch")
            if dispatch is None or response is None:
                issues.append({"code": "decision_unresolved", "index": index})
                continue
            row = validate_observation(dispatch["observation"])
            if dispatch["time_s"] != row["time_s"]:
                raise ValueError("dispatch_clock_mismatch")
            elapsed = dispatch["time_s"]-request["observation"]["time_s"]
            deadline = request["decision_deadline_s"]
            if (request["schema"] != "missionos.starship_director_request.v5"
                    or type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or not request["observation"]["time_s"] < deadline <= request["observation"]["time_s"]+expected_envelope["decision_expiry_s"]):
                raise ValueError("invalid_decision_deadline")
            if request["point"] == "deployment_start":
                cutoffs = [e["time_s"] for e in managed["events"] if e["event"] == "orbit_cutoff_command"]
                if len(cutoffs) != 1:
                    raise ValueError("initial_deadline_orbit_anchor")
                scheduled = cutoffs[0]+study["profile"]["guidance"]["orbit_release_delay_s"]-study["profile"]["integration"]["coast_dt_s"]
                if deadline != min(scheduled, request["observation"]["time_s"]+expected_envelope["decision_expiry_s"]):
                    raise ValueError("initial_deadline_schedule_mismatch")
            if request["point"] == "deployment_reassessment":
                expires = request["observation"]["hold_expires_at_s"]
                if (hold_dispatch is None or expires != hold_dispatch+expected_envelope["maximum_hold_s"]
                        or request["observation"]["sequencer_state"] != "held"
                        or request["observation"]["time_s"] < hold_dispatch+expected_envelope["hold_reassessment_after_s"]
                        or request["observation"]["time_s"] >= expires or deadline > expires):
                    raise ValueError("hold_reassessment_binding")
            if response.get("mode") == "timeout_fallback" and dispatch["time_s"] < deadline:
                raise ValueError("premature_timeout")
            reason = check_action(expected_envelope, request["point"], response.get("action"), row,
                                  elapsed_s=elapsed, observation_requests=reads, hold_used_s=hold)
            if dispatch["time_s"] >= deadline:
                reason = "decision_deadline_reached"
            if response.get("action") not in request["allowed_actions"]:
                reason = reason or "outside_request_choices"
            bound = (response.get("request_id") == request["request_id"] and response.get("request_sha256") == digest(request)
                     and response.get("mode") == expected_envelope["mode"])
            reason = reason or (None if bound else "response_binding_mismatch")
            if dispatch["rules_accepted"] is not (reason is None) or dispatch["rejection"] != reason:
                raise ValueError("independent_rules_replay_mismatch")
            action_type_valid = type(response.get("action")) is str
            if (dispatch.get("escalation_requested") is not (reason == "outside_approved_choices" and action_type_valid)
                    or dispatch.get("response_invalid") is not (not action_type_valid)):
                raise ValueError("referral_semantics_mismatch")
            fallback = fallback_action(expected_envelope, request["point"], row, observation_requests=reads, hold_used_s=hold)
            fallback_reason = check_action(expected_envelope, request["point"], fallback, row, elapsed_s=0,
                observation_requests=reads, hold_used_s=hold) if reason else None
            if (dispatch.get("fallback_rules_accepted") is not (reason is not None and fallback_reason is None)
                    or dispatch.get("fallback_rejection") != fallback_reason):
                raise ValueError("fallback_not_independently_rechecked")
            if dispatch["action"] != (fallback if reason else response["action"]):
                raise ValueError("dispatch_action_mismatch")
            action = dispatch["action"]
            reads += action == "collect_status"
            hold += 30 if action == "hold" else 0
            if action == "hold":
                hold_dispatch = dispatch["time_s"]
            later = item["later_observation"]
            if later is None or validate_observation(later)["time_s"] <= dispatch["time_s"]:
                raise ValueError("missing_later_observation")
            if action == "stop_deployment" and later["sequencer_state"] != "skipped":
                raise ValueError("stop_effect_not_observed")
            expected_hold_state = row["sequencer_state"] if action == "collect_status" and row["sequencer_state"] in ("inhibited", "skipped") else "held"
            if action in ("hold", "collect_status") and later["sequencer_state"] != expected_hold_state:
                raise ValueError("hold_effect_not_observed")
            if action == "hold" and later["hold_expires_at_s"] != dispatch["time_s"]+expected_envelope["maximum_hold_s"]:
                raise ValueError("hold_expiry_mismatch")
            events = managed["booster_run"]["events"] if request["point"] == "booster_selection" else managed["events"]
            # Observation inventory must agree with independently checked
            # separation events, not merely with another model-facing row.
            for observation in (request["observation"], row, later):
                released = sum(e["event"] == "payload_released" and e["time_s"] < observation["time_s"]
                               for e in events)
                if observation["released_count"] != released:
                    raise ValueError("observed_release_inventory_mismatch")
            if request["point"] == "deployment_start" and any(
                    e["event"] == "payload_released" and e["time_s"] < dispatch["time_s"] for e in events):
                raise ValueError("release_before_start_decision")
            if not any(e["event"] in ("managed_mission_command", "managed_booster_command")
                       and e.get("action") == action and e["time_s"] == dispatch["time_s"] for e in events):
                raise ValueError("execution_event_missing")
            if action in ("state_return", "inhibit_return", "halt_unresolved_return"):
                expected = "trimmed_state_terminal_v4" if action == "state_return" else "fixed_v1"
                if managed["retained_return"]["policy_id"] != expected:
                    raise ValueError("return_selection_not_applied")
                from .starship_return_feasibility_verifier import verify as verify_return_margin, load_certificate
                certificate, identity = load_certificate()
                if certificate is not None and certificate.get("source_sha256"):
                    from .starship_return_feasibility_verifier import registered
                    if not registered(study["profile"], identity):
                        certificate = None
                checked = verify_return_margin(row["numerical_tools"].get("return_feasibility"), certificate,
                    identity, time_s=row["time_s"], fuel_kg=row["fuel_kg"], released_count=row["released_count"])
                if not checked["passed"]:
                    raise ValueError("independent_return_margin_failed")
                _bind_return_observation(managed, study["profile"], row)
                if action == "state_return" and not dispatch["return_feasibility"]["return_admitted"]:
                    raise ValueError("unqualified_return_dispatched")
                if action == "inhibit_return":
                    if not dispatch["return_feasibility"]["bounded_coast_admitted"]:
                        raise ValueError("unchecked_coast_dispatched")
                    if any(e["event"] == "return_requested" for e in managed["events"]):
                        raise ValueError("inhibited_return_executed")
                    final_time = managed["samples"][-1]["time_s"]
                    if not 30.-1e-6 <= final_time-dispatch["time_s"] <= 30.+study["profile"]["integration"]["coast_dt_s"]+1e-6:
                        raise ValueError("bounded_return_coast_duration_mismatch")
        if reads != record["observation_requests"] or hold != record["hold_used_s"]:
            raise ValueError("resource_accounting")
        _sequence_effects(managed, record["records"], expected_envelope, study["profile"])
        for run in (baseline, managed):
            _return_execution_checks(run, study["profile"], expected_envelope)
        if expected_response_fault in COMPOUND_FAULTS:
            by_point = {r["request"]["point"]: r for r in record["records"]}
            for point in ("deployment_start", "deployment_reassessment", "return_selection"):
                if point not in by_point:
                    raise ValueError("compound_fault_point_missing")
            if by_point["deployment_start"]["dispatch"]["action"] != "hold":
                raise ValueError("compound_hold_not_executed")
            if by_point["deployment_reassessment"]["response"].get("mode") != "timeout_fallback":
                raise ValueError("compound_reassessment_timeout_missing")
            tail = by_point["return_selection"]
            if expected_response_fault == COMPOUND_FAULTS[0] and tail["response"].get("mode") != "timeout_fallback":
                raise ValueError("compound_return_timeout_missing")
            if expected_response_fault == COMPOUND_FAULTS[1] and tail["response"].get("synthetic_response_fault") != "compound_invalid_return":
                raise ValueError("compound_invalid_return_missing")
        elif expected_response_fault:
            kind, point = expected_response_fault.split("_", 1)
            matching = [item for item in record["records"] if item["request"]["point"] == point]
            if (len(matching) != 1
                    or kind == "invalid" and matching[0]["response"].get("synthetic_response_fault") != "invalid_action"
                    or kind == "timeout" and matching[0]["response"]["mode"] != "timeout_fallback"
                    or kind == "hold" and (matching[0]["response"].get("synthetic_response_fault") != "hold_action"
                        or matching[0]["dispatch"]["action"] != "hold" or not matching[0]["dispatch"]["rules_accepted"])):
                raise ValueError("response_fault_not_exercised")
        for run in study["runs"]:
            if not all(math.isfinite(x) for x in metrics(run).values() if type(x) in (int, float)):
                raise ValueError("nonfinite_outcome")
        compared = comparison(expected_case, baseline, managed)
        if study["comparison"] != compared:
            raise ValueError("comparison_mismatch")
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        issues.append({"code": str(exc)})
        compared = None
    fingerprints = {name: sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in (
        "starship_mission_director_verifier.py", "starship_mission_director.py", "starship_sixdof_verifier.py",
        "starship_retained_return_verifier.py", "starship_splashdown_verifier.py", "starship_splashdown.py", "starship_wind_verifier.py")}
    return {"schema": "missionos.starship_managed_verification.v2", "study_canonical_sha256": digest(study) if not issues else None,
            "expected_run_id": expected_run_id, "observed_run_id": observed_run_id, "verifier_source_sha256": fingerprints,
            "passed": not issues, "issues": issues, "trajectories": physical, "comparison": compared,
            "mission_completed": False, "physical_execution": False, "model_value_demonstrated": False}

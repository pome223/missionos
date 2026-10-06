"""Read-only checks of mission decisions against saved events and trajectories.

Stored-record validity, physical mission success and comparative acceptance are
three different results. This verifier never asks a model to grade its answer.
"""
from __future__ import annotations

import math

from .starship_mission_director import RESPONSE_FAULTS, contract, digest, check_action, fallback_action, validate_observation
from .starship_sixdof_verifier import verify_study
from .starship_retained_return_verifier import verify_retained_return


def metrics(run):
    contact = run["outcome"].get("contact_receipt")
    return {"released": run["outcome"]["payload_released_count"],
            "orbit": run["outcome"]["orbit_gate_reached"], "termination": run["outcome"]["termination"],
            "contact_speed_mps": contact["surface_relative_speed_mps"] if contact else None,
            "remaining_fuel_kg": run["final_state"]["propellant_kg"],
            "return_time_s": next((e["time_s"] for e in run["events"] if e["event"] == "return_requested"), None),
            "booster_destination": run["booster_run"].get("active_return_site", {}).get("site_id"),
            "booster_destination_reached": run["booster_run"].get("divert_destination_reached", False)}


def comparison(case, baseline, managed):
    a, b = metrics(baseline), metrics(managed)
    same = (a == b)
    # Frozen before running: compare actual outcomes, not whether the provider
    # happened to be called. Deployment count alone is not success after an
    # explicit suspension notice. Preserve fuel and contact tradeoffs as data.
    if case == "normal":
        accepted = same and all(r["dispatch"]["action"] in ("continue", "fixed_return", "divert")
                               for r in managed["mission_director"]["records"] if r["dispatch"])
    else:
        speed_a, speed_b = a["contact_speed_mps"], b["contact_speed_mps"]
        accepted = (a["orbit"] == b["orbit"] and a["return_time_s"] == b["return_time_s"]
                    and b["booster_destination"] == "divert"
                    and speed_a is not None and speed_b is not None and speed_b <= speed_a+.01)
        if case == "operations_notice":
            accepted = accepted and b["released"] <= 1
        else:
            accepted = accepted and b["released"] >= a["released"]
    return {"baseline": a, "managed": b, "normal_outcomes_equal": same if case == "normal" else None,
            "comparison_accepted": bool(accepted), "comparison_is_model_value_proof": False,
            "human_workload_measured": False, "physical_success_claimed": False}


def verify(study, *, expected_case, expected_envelope, expected_run_id=None, expected_response_fault=None):
    issues, physical = [], []
    try:
        if (study.get("schema") != "missionos.starship_managed_study.v1" or study.get("case") != expected_case
                or study.get("envelope") != expected_envelope or expected_envelope != contract(expected_envelope["mode"])
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
        if record["envelope"] != expected_envelope or len(record["records"]) > 5:
            raise ValueError("envelope_or_decision_budget")
        seen, reads, hold = set(), 0, 0.
        for index, item in enumerate(record["records"]):
            request, dispatch, response = item["request"], item["dispatch"], item["response"]
            if request["request_id"] in seen or request["envelope_sha256"] != digest(expected_envelope):
                raise ValueError("request_binding")
            if expected_run_id and not request["request_id"].startswith(expected_run_id+":"):
                raise ValueError("cross_run_response")
            seen.add(request["request_id"])
            validate_observation(request["observation"])
            if dispatch is None or response is None:
                issues.append({"code": "decision_unresolved", "index": index})
                continue
            row = validate_observation(dispatch["observation"])
            if dispatch["time_s"] != row["time_s"]:
                raise ValueError("dispatch_clock_mismatch")
            elapsed = dispatch["time_s"]-request["observation"]["time_s"]
            deadline = request["decision_deadline_s"]
            if (request["schema"] != "missionos.starship_director_request.v2"
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
            if response.get("mode") == "timeout_fallback" and dispatch["time_s"] < deadline:
                raise ValueError("premature_timeout")
            reason = check_action(expected_envelope, request["point"], response.get("action"), row,
                                  elapsed_s=elapsed, observation_requests=reads, hold_used_s=hold)
            if dispatch["time_s"] >= deadline:
                reason = reason or "decision_deadline_reached"
            bound = (response.get("request_id") == request["request_id"] and response.get("request_sha256") == digest(request)
                     and response.get("mode") == expected_envelope["mode"])
            reason = reason or (None if bound else "response_binding_mismatch")
            if dispatch["rules_accepted"] is not (reason is None) or dispatch["rejection"] != reason:
                raise ValueError("independent_rules_replay_mismatch")
            fallback = fallback_action(expected_envelope, request["point"], row, observation_requests=reads, hold_used_s=hold)
            if dispatch["action"] != (fallback if reason else response["action"]):
                raise ValueError("dispatch_action_mismatch")
            action = dispatch["action"]
            reads += action == "collect_status"
            hold += 30 if action == "hold" else 0
            later = item["later_observation"]
            if later is None or validate_observation(later)["time_s"] <= dispatch["time_s"]:
                raise ValueError("missing_later_observation")
            if action == "stop_deployment" and later["sequencer_state"] != "skipped":
                raise ValueError("stop_effect_not_observed")
            if action in ("hold", "collect_status") and later["sequencer_state"] != "held":
                raise ValueError("hold_effect_not_observed")
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
            if action in ("retained_return", "fixed_return"):
                expected = "mass_state_terminal_v3" if action == "retained_return" else "fixed_v1"
                if managed["retained_return"]["policy_id"] != expected:
                    raise ValueError("return_selection_not_applied")
        if reads != record["observation_requests"] or hold != record["hold_used_s"]:
            raise ValueError("resource_accounting")
        if expected_response_fault:
            kind, point = expected_response_fault.split("_", 1)
            matching = [item for item in record["records"] if item["request"]["point"] == point]
            if len(matching) != 1 or (kind == "invalid" and matching[0]["response"].get("synthetic_response_fault") != "invalid_action") or (kind == "timeout" and matching[0]["response"]["mode"] != "timeout_fallback"):
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
    return {"passed": not issues, "issues": issues, "trajectories": physical, "comparison": compared,
            "mission_completed": False, "physical_execution": False, "model_value_demonstrated": False}

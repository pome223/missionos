"""Independent saved-record checks, not certification of faulted recovery."""
from copy import deepcopy
import math

from .starship_trouble_control import authority, digest, validate_observation, validate_request, valid_live_response
from .starship_mission_director import fuel_sensor
from .starship_trouble_verifier import verify_run as verify_faults


def verify_run(run, profile, expected_authority):
    errors = []
    diagnostics_completed = True
    def require(condition, code):
        if not condition:
            errors.append(code)
    try:
        control = run["trouble_control"]
        grant = control["authority"]
        require(grant == expected_authority, "authority_differs_from_approved_plan")
        require(grant == authority(profile, run["trouble"]["manifest"], grant["source_sha256"], grant["duration_s"], grant["mode"], provider_models=grant["provider_models"]), "noncanonical_authority")
        require(control["fault_return_qualified"] is False and control["physical_execution"] is False, "claim_boundary_changed")
        fault_check = verify_faults(run)
        require(fault_check["passed"], "fault_record_inconsistent")
        bodies = {"ship": run, "booster": run.get("booster_run")}
        witnesses = control["observation_witnesses"]
        matched = {}
        for index, witness in enumerate(witnesses):
            row = validate_observation(witness["observation"])
            body = bodies[row["body"]]
            require(body is not None, f"missing_observed_body:{index}")
            candidates = [sample for sample in body["samples"] if sample.get("trouble_control_observation") == row]
            require(bool(candidates), f"observation_not_in_plant_record:{index}")
            if not candidates:
                continue
            state = witness["state"]
            state_keys = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg", "engine_states", "flap_angles_rad")
            require(any(all(sample[k] == state[k] for k in state_keys) for sample in candidates), f"forged_state_witness:{index}")
            sample = candidates[0]
            require(sample["phase"] == row["phase"] and sample["time_s"] == row["time_s"], f"phase_or_clock_mismatch:{index}")
            require(row["altitude_m"] == sample["altitude_m"], f"altitude_mismatch:{index}")
            require(math.isclose(row["speed_mps"], math.sqrt(sum(x*x for x in sample["v_eci_mps"])), abs_tol=1e-9), f"speed_mismatch:{index}")
            require(math.isclose(row["body_rate_rad_s"], math.sqrt(sum(x*x for x in sample["omega_body_rad_s"])), abs_tol=1e-12), f"rate_mismatch:{index}")
            require(row["fuel_kg"] == fuel_sensor(sample["propellant_kg"], sample["time_s"], row["body"]), f"fuel_mismatch:{index}")
            require(row["component_health"] == sample["controller"]["component_health"], f"diagnostic_mismatch:{index}")
            matched[digest(row)] = row
        commands = {body: [event for event in value["events"] if event["event"] == "trouble_control_command"]
                    for body, value in bodies.items() if value is not None}
        seen = set()
        applied = {body: [] for body in commands}
        live_decisions = 0
        for index, record in enumerate(control["records"]):
            req = validate_request(record["request"], grant)
            row, later = validate_observation(record["observation"]), record["later_observation"]
            require(req["request_id"] not in seen, f"duplicate_request:{index}")
            seen.add(req["request_id"])
            require(all(digest(value) in matched for value in (req["observation"], row)), f"unwitnessed_decision:{index}")
            require(req["observation"]["time_s"] < row["time_s"] == record["time_s"], f"decision_clock:{index}")
            require(req["observation"]["body"] == row["body"], f"decision_body:{index}")
            history = []
            for earlier in control["records"][:index]:
                if earlier["observation"]["body"] != row["body"] or earlier["time_s"] > req["observation"]["time_s"]:
                    continue
                diagnostic = earlier.get("diagnostic_observation")
                completed = diagnostic is not None and diagnostic["time_s"] <= req["observation"]["time_s"]
                irrelevant = {"observed_at_s"} | ({"payload_mechanism"} if row["body"] == "booster" else set())
                old_health = {k:v for k,v in earlier["observation"]["component_health"].items() if k not in irrelevant}
                new_health = {k:v for k,v in diagnostic["component_health"].items() if k not in irrelevant} if completed else None
                history.append({"time_s": earlier["time_s"], "action": earlier["action"],
                    "diagnostic_status": "completed" if completed else "pending" if earlier["action"] == "collect_status" else "not_requested",
                    "diagnostic_health_unchanged": new_health == old_health if completed else None})
            require(req["recent_actions"] == history[-grant["recent_action_limit"]:], f"forged_action_history:{index}")
            action = record["action"]
            # Re-evaluate the small authority independently from check_action.
            require(action in grant["actions"], f"unapproved_action:{index}")
            if action == "withhold_deployment":
                require(row["body"] == "ship" and row["phase"] in ("stack_ascent", "ship_ascent", "orbital_coast") and row["released_count"] < 26, f"invalid_withhold:{index}")
            if action == "inhibit_return":
                require(row["body"] == "ship" and row["phase"] == "orbital_coast" and row["return_deadline_s"] is not None and row["time_s"] < row["return_deadline_s"], f"invalid_return_inhibit:{index}")
            if action == "collect_status":
                require(row["phase"] not in ("landing_burn", "booster_landing_burn")
                    and min(grant["duration_s"], row["return_deadline_s"] or float("inf"))-row["time_s"] >= 2., f"invalid_diagnostic_window:{index}")
            else:
                require(record.get("diagnostic_completion") == {"status": "not_requested", "time_s": None, "reason": None}, f"unexpected_diagnostic_completion:{index}")
            require(record["fallback_rejection"] is None, f"rejected_fallback_executed:{index}")
            if record["fallback"] is False:
                response = record["response"]
                require(record["rejection"] is None and record["accepted_action"] == action == response["action"], f"acceptance_mismatch:{index}")
                require(response["request_id"] == req["request_id"] and response["request_sha256"] == digest(req) and response["mode"] == grant["mode"], f"response_binding:{index}")
                require(row["time_s"] < req["deadline_s"], f"late_response:{index}")
                if action == "continue_mission":
                    old, new = deepcopy(req["observation"]["component_health"]), deepcopy(row["component_health"])
                    old.pop("observed_at_s")
                    new.pop("observed_at_s")
                    if row["body"] == "booster":
                        old.pop("payload_mechanism")
                        new.pop("payload_mechanism")
                    require(old == new and row["phase"] == req["observation"]["phase"], f"stale_continue:{index}")
                if grant["mode"] == "live":
                    confirmed = valid_live_response(response, req, grant)
                    require(confirmed, f"invalid_live_invocation_evidence:{index}")
                    receipt = response["selected_invocation"]
                    require(receipt.get("status") == "succeeded" and receipt.get("model_inference_invoked") is True
                            and receipt in (response["jev_invocation"], response["llm_invocation"]), f"unconfirmed_model:{index}")
                    live_decisions += int(confirmed)
                    from src.intelligence.starship_trouble_agent import evidence_table, public_input
                    public = public_input(req, grant)
                    require(receipt.get("public_input_sha256") == digest(public), f"model_input_binding:{index}")
                    expected_payload = req["observation"]["component_health"]["payload_mechanism"] if row["body"] == "ship" else "not_applicable"
                    require(public["observation"]["component_health"]["payload_mechanism"] == expected_payload
                        and public["evidence"]["payload_mechanism"] == expected_payload, f"model_diagnostic_applicability:{index}")
                    table = evidence_table(req["observation"])
                    require(response.get("evidence_values") == {key: table[key] for key in response["evidence_ids"]}, f"evidence_reference_mismatch:{index}")
            else:
                require(record["accepted_action"] is None and record["rejection"] is not None, f"fallback_disguised_as_model:{index}")
                health = row["component_health"]
                faulty = (any(not item["available"] for item in health["engine_health"])
                    or any(item["jammed"] for item in health["actuator_health"])
                    or row["body"] == "ship" and health["payload_mechanism"] == "blocked" or not health["tower_ready"])
                can_withhold = row["body"] == "ship" and row["phase"] in ("stack_ascent", "ship_ascent", "orbital_coast") and row["released_count"] < 26
                require(action == ("withhold_deployment" if faulty and can_withhold else "continue_mission"), f"noncanonical_fallback:{index}")
            body = row["body"]
            applied[body].append((record["time_s"], action))
            if action == "collect_status" and record.get("diagnostic_observation") is None:
                diagnostics_completed = False
            require(later is not None, f"missing_later_observation:{index}")
            if later is None:
                continue
            later = validate_observation(later)
            require(digest(later) in matched and later["body"] == body and later["time_s"] > row["time_s"], f"forged_later_observation:{index}")
            if action == "withhold_deployment":
                require(later["deployment_withheld"] is True and later["sequencer_state"] == "skipped", f"withhold_not_observed:{index}")
                require(not any(sat["release_state"]["time_s"] > row["time_s"] for sat in run["satellites"]), f"release_after_withhold:{index}")
            if action == "inhibit_return":
                require(later["return_inhibited"] is True, f"return_inhibit_not_observed:{index}")
                require(not any(sample["time_s"] > row["time_s"] and sample["phase"] in ("deorbit_slew", "deorbit_burn", "ballistic_return", "landing_burn") for sample in run["samples"]), f"deorbit_after_inhibit:{index}")
            if action == "collect_status":
                diagnostic = record.get("diagnostic_observation")
                completion = record.get("diagnostic_completion")
                if diagnostic is not None:
                    diagnostic = validate_observation(diagnostic)
                    require(digest(diagnostic) in matched and diagnostic["body"] == body
                            and diagnostic["time_s"] >= row["time_s"]+grant["diagnostic_delay_s"], f"forged_diagnostic_completion:{index}")
                    require(completion == {"status": "completed", "time_s": diagnostic["time_s"], "reason": None}, f"diagnostic_completion_state:{index}")
                else:
                    diagnostics_completed = False
                    outcome = bodies[body]["outcome"]
                    end = bodies[body]["final_state"]["time_s"]
                    require(outcome["termination"] == "surface_contact"
                        and row["time_s"] <= end < row["time_s"]+grant["diagnostic_delay_s"]
                        and completion == {"status": "interrupted", "time_s": end, "reason": "body_surface_contact",
                            "outcome_sha256": digest(outcome)}, f"unproved_diagnostic_interruption:{index}")
        for body, events in commands.items():
            require(applied[body] == [(event["time_s"], event["action"]) for event in events], f"executor_event_mismatch:{body}")
        for pending in control["pending_requests"]:
            validate_request(pending, grant)
            require(digest(pending["observation"]) in matched, "unwitnessed_pending_request")
        require(control["deployment_withheld"] == any(action == "withhold_deployment" for _, action in applied.get("ship", [])), "final_withhold_latch")
        require(control["return_inhibited"] == any(action == "inhibit_return" for _, action in applied.get("ship", [])), "final_return_latch")
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        errors.append("malformed_control_record:"+type(error).__name__)
        live_decisions = 0
    return {"passed": not errors, "errors": errors, "accepted_live_decisions": live_decisions,
        "all_requested_diagnostics_completed": diagnostics_completed,
        "case_accepted": not errors and diagnostics_completed,
        "claim": "authority_and_record_consistency_not_recovery_certification_or_explanation_correctness"}

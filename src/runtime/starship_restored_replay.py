"""Saved-only display mapping for one accepted restored native mission.

No physics, controller, predictor, browser, filesystem or provider is imported.
The external caller pins all three evidence blobs. This adapter checks their
saved identities and seams; it does not re-execute their physical verifier or
authenticate an operator/HMAC. Its output is an intermediate display dataset,
not a rendered video, a predicted trajectory or additional mission evidence.
"""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json
import math
import re

SCHEMA = "missionos.starship_restored_replay.v1"
DEFAULT_MAXIMUM_INPUT_BYTES = 128*1024**2
HARD_MAXIMUM_INPUT_BYTES = 512*1024**2
STATE = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
    "propellant_kg", "engine_states", "flap_angles_rad")
DISPLAY = (*STATE, "phase", "body_id", "altitude_m", "ground_speed_mps", "com_z_m", "mass_kg",
    "applied_thrust_n", "engine_count", "throttle", "contact", "aero_panel_names", "main_engine_anchors", "main_engine_thrust_n")
BOUNDARIES = {"stage_separation", "payload_released", "ascent_screen_engine_unavailable"}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_wire(value)).hexdigest()


def _same(first, second):
    return _wire(first) == _wire(second)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _input_budget(maximum_input_bytes):
    _require(type(maximum_input_bytes) is int and 0 < maximum_input_bytes <= HARD_MAXIMUM_INPUT_BYTES,
        "restored_replay_invalid_input_byte_budget")
    return maximum_input_bytes


def _read(raw, expected, label, *, maximum_input_bytes=DEFAULT_MAXIMUM_INPUT_BYTES):
    maximum_input_bytes = _input_budget(maximum_input_bytes)
    _require(type(expected) is str and re.fullmatch(r"[a-f0-9]{64}", expected)
        and type(raw) is bytes and 0 < len(raw) <= maximum_input_bytes
        and sha256(raw).hexdigest() == expected, "restored_replay_"+label+"_raw_hash_or_budget")
    def pairs(items):
        result = dict(items)
        _require(len(items) == len(result), "restored_replay_duplicate_json_key")
        return result
    def constant(_value):
        raise ValueError("restored_replay_nonfinite_json")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    _wire(value)
    return value


def _state(row):
    _require(type(row) is dict and all(key in row for key in STATE)
        and _number(row["time_s"]) and 0 <= row["time_s"] <= 16000.
        and _number(row["propellant_kg"]) and row["propellant_kg"] >= 0,
        "restored_replay_complete_finite_saved_state_required")
    for key, length in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(type(row[key]) is list and len(row[key]) == length and all(_number(value) for value in row[key]),
            "restored_replay_invalid_saved_vector")
    _require(abs(sum(value*value for value in row["q_body_to_eci"])-1.) <= 1e-6,
        "restored_replay_nonunit_saved_quaternion")
    _require(type(row["engine_states"]) is list and type(row["flap_angles_rad"]) is list
        and all(_number(value) for value in row["flap_angles_rad"]), "restored_replay_actual_actuators_required")
    for engine in row["engine_states"]:
        _require(type(engine) is dict and set(engine) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(engine["available"]) is bool and all(_number(engine[key]) for key in
                ("throttle", "gimbal_x_rad", "gimbal_y_rad")), "restored_replay_invalid_actual_engine_state")
    return {key: row[key] for key in STATE}


def _events(rows, source, minimum, maximum):
    _require(type(rows) is list, "restored_replay_saved_events_required")
    result, previous = [], minimum
    for index, row in enumerate(rows):
        _require(type(row) is dict and _number(row.get("time_s")) and previous <= row["time_s"] <= maximum
            and type(row.get("event")) is str, "restored_replay_event_clock_or_shape")
        result.append({"time_s": row["time_s"], "event": row["event"], "kind": "recorded_physical_event",
            "detail": row.get("detail", ""), "source": {"record": "mission", "pointer": f"{source}/{index}"}})
        previous = row["time_s"]
    return result


def _track(body_id, parts, events):
    """Every supplied point survives; equal clocks start separate segments."""
    segments, previous, previous_source = [], None, None
    for source, samples in parts:
        _require(type(samples) is list and samples, "restored_replay_nonempty_saved_samples_required")
        for index, sample in enumerate(samples):
            actual = _state(sample)
            _require(type(sample.get("phase")) is str, "restored_replay_saved_phase_required")
            point = {key: deepcopy(sample[key]) for key in DISPLAY if key in sample}
            point["source"] = {"record": "mission", "pointer": f"{source}/{index}"}
            time = actual["time_s"]
            _require(previous is None or time >= previous["time_s"], "restored_replay_reversed_saved_clock")
            transition = None
            if previous is not None and time == previous["time_s"]:
                changed = [key for key in STATE if not _same(actual[key], previous[key])]
                names = [row["event"] for row in events if row["time_s"] == time and row["event"] in BOUNDARIES]
                _require(not changed or names, "restored_replay_unexplained_same_clock_state_change")
                transition = {"time_s": time, "kind": "saved_discontinuity" if changed else "same_clock_saved_boundary",
                    "before_source": previous_source, "after_source": point["source"], "changed_state_fields": changed,
                    "recorded_event_names": names, "both_saved_states_retained": True, "interpolation_across_boundary": False}
            if not segments or transition is not None:
                segments.append({"index": len(segments), "boundary_before": transition, "samples": []})
            segments[-1]["samples"].append(point)
            previous, previous_source = actual, point["source"]
    return {"body_id": body_id, "segments": segments, "start_time_s": segments[0]["samples"][0]["time_s"],
        "end_time_s": previous["time_s"], "sample_count": sum(len(row["samples"]) for row in segments),
        "continuous_segment_clocks_strictly_increasing": True, "saved_points_removed": 0,
        "after_last_sample": "record_ended_no_extrapolation", "attitude_source": "saved_scalar_first_hamilton_quaternion",
        "actuator_display": "last_saved_actual_sample_not_command_inference"}


def _review_route(request, attempts, policy):
    """Display binding only: independently recompute the saved routing facts."""
    first = attempts[0].get("judgment") if attempts else None
    result = {"schema": "missionos.starship_restored_return_review_route.v1", "policy_sha256": _digest(policy),
        "route": "not_evaluated", "review_required": False, "metrics_are_uncalibrated": True,
        "confidence": None, "selected_action_probability": None, "maximum_other_probability": None,
        "selected_action_margin": None, "low_confidence": False, "small_selected_margin": False}
    if request["phase"] != "return":
        result["route"] = "not_applicable"
    elif first is None:
        pass
    elif (type(first) is not dict or first.get("status") != "valid" or first.get("provider") != "typesafe"
            or first.get("action") not in request["allowed_actions"]):
        result["route"] = "no_valid_jev_judgment"
    elif first["action"] == "request_deep_reasoning":
        result.update(route="explicit_jev_delegation", review_required=True)
    else:
        receipt = first.get("provider_receipt", {})
        receipt = receipt if type(receipt) is dict else {}
        probabilities, confidence = receipt.get("native_choice_probabilities"), receipt.get("native_choice_confidence")
        if (receipt.get("status") != "succeeded" or receipt.get("validation_error") is not None
                or type(probabilities) is not dict or set(probabilities) != set(request["allowed_actions"])
                or not all(_number(value) and 0 <= value <= 1 for value in [*probabilities.values(), confidence])
                or not math.isclose(sum(probabilities.values()), 1., abs_tol=.001)):
            result["route"] = "invalid_jev_metrics"
        else:
            selected = probabilities[first["action"]]
            other = max((value for action, value in probabilities.items() if action != first["action"]), default=0.)
            margin = Decimal(str(selected))-Decimal(str(other))
            low = Decimal(str(confidence)) < Decimal(str(policy["confidence_floor"]))
            small = margin < Decimal(str(policy["selected_action_margin_floor"]))
            result.update(route="rules_uncertainty_review" if low or small else "jev_final", review_required=low or small,
                confidence=confidence, selected_action_probability=selected, maximum_other_probability=other,
                selected_action_margin=float(margin), low_confidence=low, small_selected_margin=small)
    return result


def _review_projection(mission, host, verified):
    """Require the original V2 policy and route, not presentation assertions."""
    execution, governance = mission["execution"], mission["governance"]
    client, decision_host = governance.get("decision_client", {}), host["decision_host"]
    policy = execution.get("return_review_policy")
    if policy is None:
        _require(all(not {"return_review_policy", "return_review_policy_sha256"}.intersection(value)
                for value in (execution, host, client, decision_host)) and "return_review" not in verified
            and client.get("schema") != "missionos.starship_restored_decision_client.v2"
            and decision_host.get("schema") != "missionos.starship_restored_decision_host.v2"
            and all("return_review" not in value and value.get("schema") != "missionos.starship_restored_decision_exchange.v2"
                for value in client.get("results", []))
            and all(session["frame"].get("schema") != "missionos.starship_restored_decision_pipe_request.v2"
                and "return_review_policy_sha256" not in session["frame"]
                and all("return_review" not in value and value.get("schema") != "missionos.starship_restored_decision_exchange.v2"
                    for value in (session.get("delivered_result") or {}, session.get("thread_result") or {}))
                for session in decision_host["requests"]), "restored_replay_undeclared_return_review")
        return None
    expected = {"schema": "missionos.starship_restored_return_review_policy.v1", "confidence_floor": .5,
        "selected_action_margin_floor": .1}
    _require(_same(policy, expected) and all(_number(policy[key]) for key in ("confidence_floor", "selected_action_margin_floor")),
        "restored_replay_return_review_policy_changed")
    _require("src/runtime/starship_restored_return_review.py" in execution["source_sha256"],
        "restored_replay_return_review_source_marker_missing")
    policy_hash = _digest(policy)
    _require(all(_same(value.get("return_review_policy"), policy) and value.get("return_review_policy_sha256") == policy_hash
        for value in (execution, host, client, decision_host))
        and client.get("schema") == "missionos.starship_restored_decision_client.v2"
        and decision_host.get("schema") == "missionos.starship_restored_decision_host.v2",
        "restored_replay_return_review_policy_binding_changed")
    final = {value["request_id"]: value for value in client["results"]}
    _require(len(final) == len(client["results"]) == len(decision_host["requests"]),
        "restored_replay_return_review_request_inventory_changed")
    rows, triggered, explicit, completed, unresolved = {}, 0, 0, 0, 0
    for index, session in enumerate(decision_host["requests"]):
        frame, attempts = session["frame"], session["attempts"]
        request = frame["request"]
        exchange = session.get("delivered_result")
        _require(frame.get("schema") == "missionos.starship_restored_decision_pipe_request.v2"
            and frame.get("return_review_policy_sha256") == policy_hash and frame.get("request_sha256") == _digest(request)
            and type(attempts) is list and len(attempts) <= 2 and type(exchange) is dict
            and exchange.get("schema") == "missionos.starship_restored_decision_exchange.v2"
            and exchange.get("request_id") == request["request_id"] and exchange.get("request_sha256") == _digest(request)
            and _same(exchange, final.get(request["request_id"])) and _same(exchange.get("attempts"), attempts)
            and _same(exchange.get("judgment"), attempts[-1].get("judgment") if attempts else None),
            "restored_replay_return_review_exchange_binding_changed")
        proof = _review_route(request, attempts, policy)
        _require(_same(exchange.get("return_review"), proof), "restored_replay_return_review_route_changed")
        if len(attempts) == 2:
            _require(proof["review_required"] and attempts[0]["provider"] == "typesafe" and attempts[1]["provider"] == "deepseek",
                "restored_replay_return_review_provider_order_changed")
        _require(all(attempt.get("judgment") is None or attempt["judgment"].get("provider") == attempt["provider"]
            for attempt in attempts), "restored_replay_return_review_provider_judgment_changed")
        is_complete = (len(attempts) == 2 and exchange.get("status") == "valid"
            and type(exchange.get("judgment")) is dict and exchange["judgment"].get("status") == "valid"
            and exchange.get("action") == exchange["judgment"].get("action"))
        explicit += int(proof["route"] == "explicit_jev_delegation")
        if proof["route"] == "rules_uncertainty_review":
            triggered += 1
            completed += int(is_complete)
            unresolved += int(not is_complete)
        rows[index] = {"proof": proof, "status": exchange["status"], "action": exchange.get("action"),
            "first": attempts[0].get("judgment") if attempts else None,
            "proof_source": {"record": "host", "pointer": f"/decision_host/requests/{index}/delivered_result/return_review"}}
    summary = {"schema": "missionos.starship_restored_return_review_verification.v1", "policy_sha256": policy_hash,
        "policy_triggered_count": triggered, "explicit_jev_delegation_count": explicit,
        "completed_policy_review_count": completed, "unresolved_policy_review_count": unresolved,
        "original_jev_judgments_preserved": True, "review_is_approval": False}
    _require(_same(verified.get("return_review"), summary), "restored_replay_return_review_verified_summary_changed")
    return rows


def _judgments(host, review=None):
    rows = []
    for index, session in enumerate(host["decision_host"]["requests"]):
        request = session["frame"]["request"]
        for number, attempt in enumerate(session["attempts"]):
            judgment = attempt.get("judgment")
            receipt = {} if judgment is None else judgment.get("provider_receipt", {})
            fixture = receipt.get("mode") in ("fixture", "synthetic_fixture")
            invoked = receipt.get("model_inference_invoked")
            classification = "fixed_response" if fixture else "confirmed_native_inference" if invoked is True else "unconfirmed_or_failed"
            row = {"request_id": request["request_id"], "request_time_s": request["time_s"],
                "request_deadline_s": request["deadline_s"], "phase": request["phase"], "provider": attempt["provider"],
                "provider_model": receipt.get("model_id", receipt.get("request_model")), "classification": classification,
                "status": None if judgment is None else judgment.get("status"),
                "action": None if judgment is None else judgment.get("action"),
                "external_summary": None if judgment is None else judgment.get("summary", ""),
                "reference_ids": [] if judgment is None else [value["reference_id"] for value in judgment.get("references", [])],
                "native_choice_probabilities": receipt.get("native_choice_probabilities"),
                "native_choice_confidence": receipt.get("native_choice_confidence"),
                "free_prose_verified": False, "internal_reasoning_recorded": False,
                "resolution_simulation_time_inferred": False,
                "source": {"record": "host", "pointer": f"/decision_host/requests/{index}/attempts/{number}"}}
            if review is not None and request["phase"] == "return":
                retained = review[index]
                proof = retained["proof"]
                role = ("explicit_jev_delegation" if proof["route"] == "explicit_jev_delegation" else "provisional_jev_judgment") if number == 0 and proof["review_required"] else (
                    "reviewed_final_proposal" if number and retained["status"] == "valid" else "review_not_adopted" if number else "single_stage_proposal")
                row["return_review"] = {"attempt_index": number, "judgment_role": role,
                    "routing": deepcopy(proof), "routing_source": retained["proof_source"],
                    "final_exchange_status": retained["status"], "final_exchange_action": retained["action"],
                    "proposal_is_approval_or_dispatch": False, "earlier_provider_time_inferred": False}
                if number:
                    first = retained["first"] or {}
                    receipt = first.get("provider_receipt", {})
                    row["return_review"]["initial_jev"] = {"status": first.get("status"), "action": first.get("action"),
                        "native_choice_confidence": receipt.get("native_choice_confidence"),
                        "native_choice_probabilities": deepcopy(receipt.get("native_choice_probabilities"))}
            rows.append(row)
    return rows


def build_restored_replay(mission_raw, host_raw, verification_raw, *, expected_mission_sha256,
        expected_host_sha256, expected_verification_sha256, maximum_input_bytes=DEFAULT_MAXIMUM_INPUT_BYTES):
    """Prepare successful-showcase data from externally pinned accepted records.

    Expected hashes come from the trusted outer result, not this worker's own
    assertions. Passing here establishes display mapping only. The separately
    pinned verifier remains responsible for physical/orchestration acceptance.
    The optional bounded display-input budget defaults to 128 MiB and cannot
    exceed 512 MiB. It changes no source, state, physics or physical criterion.
    """
    maximum_input_bytes = _input_budget(maximum_input_bytes)
    mission = _read(mission_raw, expected_mission_sha256, "mission", maximum_input_bytes=maximum_input_bytes)
    host = _read(host_raw, expected_host_sha256, "host", maximum_input_bytes=maximum_input_bytes)
    verified = _read(verification_raw, expected_verification_sha256, "verification", maximum_input_bytes=maximum_input_bytes)
    _require(mission.get("schema") == "missionos.starship_restored_native_mission.v1"
        and host.get("schema") == "missionos.starship_restored_native_host.v1"
        and verified.get("schema") == "missionos.starship_restored_native_verification.v1"
        and all(verified.get(key) is True for key in ("passed", "physical_success", "ship_physical_success", "booster_physical_success")),
        "restored_replay_requires_accepted_physical_native_wrapper")
    execution, prefix, continuation, governance = (mission[key] for key in ("execution", "prefix", "return", "governance"))
    _require(execution.get("schema") == "missionos.starship_restored_native_execution.v1"
        and execution.get("mode") in ("fixture", "live") and host.get("mode") == execution["mode"]
        and _same(host.get("context"), execution["context"]) and host.get("plan_sha256") == execution["plan_sha256"]
        and host.get("source_sha256") == _digest(execution["source_sha256"])
        and _same(host.get("integration_policy"), execution["integration_policy"]), "restored_replay_host_execution_identity_changed")
    _require(type(continuation) is dict and continuation.get("schema") == "missionos.starship_restored_return_continuation.v1"
        and continuation.get("prediction_only") is False and continuation.get("actual_state_initialized_from_prediction") is False
        and type(governance) is dict and governance.get("schema") == "missionos.starship_restored_return_governance.v1"
        and prefix["restored_prefix"].get("closed") is True, "restored_replay_prediction_or_unclosed_handoff")
    handoff = prefix["restored_prefix"]["handoff_record"]
    _require(_digest(handoff) == prefix["restored_prefix"]["handoff_record_sha256"]
        and _same(handoff["source_sha256"], execution["source_sha256"])
        and handoff["profile_sha256"] == execution["profile_sha256"]
        and _same(prefix["final_state"], handoff["final_state"])
        and _same(prefix["final_state"], continuation["initial_state"])
        and _same(prefix["final_vehicle"], handoff["final_vehicle"])
        and _same(prefix["final_vehicle"], continuation["vehicle"])
        and _same(continuation["vehicle"], continuation["final_vehicle"]), "restored_replay_exact_actual_handoff_changed")
    inventory = handoff["inventory"]
    released, retained = inventory["released_count"], inventory["retained_count"]
    _require(type(released) is type(retained) is int and released+retained == 26 and retained in (0, 13, 26)
        and len(prefix["satellites"]) == released and len(inventory["release_receipts"]) == released
        and _same(inventory["release_receipts"], [row["receipt"] for row in prefix["satellites"]])
        and continuation.get("retained_count") == retained and continuation.get("payload_releases_after_origin") == 0
        and verified.get("inventory_closed") is True and verified.get("payload_released_count") == released
        and verified.get("retained_count") == retained
        and verified.get("return_candidate_id") == continuation["binding"]["candidate_id"], "restored_replay_inventory_or_reservation_changed")
    _require(_same(verified.get("ship_contact"), continuation["outcome"]["contact_metrics"]),
        "restored_replay_verified_ship_metrics_changed")
    booster = prefix["booster_run"]
    _require(_same(booster["booster_separation_state"], prefix["booster_separation_state"]),
        "restored_replay_booster_origin_changed")
    all_events = _events(prefix["events"], "/prefix/events", prefix["samples"][0]["time_s"], prefix["final_state"]["time_s"])
    all_events += _events(continuation["events"], "/return/events", continuation["initial_state"]["time_s"], continuation["final_state"]["time_s"])
    booster_events = _events(booster["events"], "/prefix/booster_run/events", booster["samples"][0]["time_s"], booster["final_state"]["time_s"])
    ship = _track("ship", [("/prefix/samples", prefix["samples"]), ("/return/samples", continuation["samples"])], all_events)
    boost = _track("booster", [("/prefix/booster_run/samples", booster["samples"])], booster_events)
    _require(ship["start_time_s"] == 0. and verified["ship_contact"].get("contact_observed") is True
        and verified["ship_contact"].get("contact_envelope_met") is True
        and verified["ship_contact"].get("event_time_s") == ship["end_time_s"],
        "restored_replay_ground_start_or_contact_endpoint_changed")
    _require(_same(_state(prefix["samples"][-1]), prefix["final_state"])
        and _same(_state(continuation["samples"][0]), continuation["initial_state"])
        and _same(_state(continuation["samples"][-1]), continuation["final_state"])
        and _same(_state(booster["samples"][0]), prefix["booster_separation_state"])
        and _same(_state(booster["samples"][-1]), booster["final_state"]), "restored_replay_saved_endpoint_changed")
    bodies, releases = [ship, boost], []
    identifiers = set()
    for index, child in enumerate(prefix["satellites"]):
        identity, receipt = child["id"], child["receipt"]
        _require(type(identity) is str and identity not in identifiers and child.get("orbital_propagation_only") is True,
            "restored_replay_duplicate_satellite_or_unsupported_propagation")
        _require(identity not in ("ship", "booster"), "restored_replay_satellite_body_identity_collision")
        identifiers.add(identity)
        _require(_same(_state(child["samples"][0]), child["release_state"])
            and child["samples"][0]["time_s"] == receipt["time_s"], "restored_replay_child_release_state_changed")
        child_track = _track(identity, [(f"/prefix/satellites/{index}/samples", child["samples"])], [])
        _require(child_track["end_time_s"] <= prefix["final_state"]["time_s"],
            "restored_replay_satellite_outside_saved_prefix_scope")
        bodies.append(child_track)
        releases.append({"time_s": receipt["time_s"], "satellite_id": identity,
            "released_count_after": index+1, "retained_count_after": 25-index,
            "source": {"record": "mission", "pointer": f"/prefix/satellites/{index}/receipt"}})
    timeline = all_events+booster_events
    for index, row in enumerate(governance["deployment"]["decisions"]):
        timeline.append({"time_s": row["applied_at_actual_time_s"], "event": "deployment_judgment_applied",
            "kind": "recorded_judgment_application", "action": row["applied_action"], "fallback_used": row["fallback_used"],
            "source": {"record": "mission", "pointer": f"/governance/deployment/decisions/{index}"}})
    for index, row in enumerate(governance["decisions"]):
        timeline.append({"time_s": row["actual_snapshot"]["state"]["time_s"], "event": "return_judgment_resolved",
            "kind": "recorded_judgment_disposition", "action": row["original_action"], "disposition": row["disposition"],
            "source": {"record": "mission", "pointer": f"/governance/decisions/{index}"}})
    for index, item in enumerate(host.get("operator_ledger", [])):
        for key, event in (("request", "operator_scope_request"), ("receipt", "operator_choice_received"),
                ("consumption", "scope_amendment_consumed")):
            value = item.get(key)
            if value is not None:
                clock_key = "issued_simulation_time_s" if key == "request" else "simulation_time_s"
                timeline.append({"time_s": value[clock_key], "event": event,
                    "kind": "recorded_operator_protocol_fact", "choice": value.get("choice"),
                    "approved": value.get("approved"), "record_sha256": value.get("sha256"),
                    "operator_identity_authenticated_by_replay": False,
                    "source": {"record": "host", "pointer": f"/operator_ledger/{index}/{key}"}})
    for index, change in enumerate(governance.get("current_origin_changes", [])):
        timeline.append({"time_s": change["actual_snapshot"]["state"]["time_s"], "event": "actual_return_reservation_changed",
            "kind": "recorded_reservation_change_not_dispatch",
            "source": {"record": "mission", "pointer": f"/governance/current_origin_changes/{index}"}})
        if change.get("confirmation_snapshot") is not None:
            timeline.append({"time_s": change["confirmation_snapshot"]["state"]["time_s"],
                "event": "later_actual_reservation_observed", "kind": "recorded_confirmation_not_contact",
                "source": {"record": "mission", "pointer": f"/governance/current_origin_changes/{index}/confirmation_snapshot"}})
    dispatch = governance["dispatch"]
    _require(dispatch.get("gate_met") is True and _same(dispatch["actual_snapshot"]["state"], continuation["return_start_state"]),
        "restored_replay_dispatch_state_or_gate_changed")
    timeline.append({"time_s": dispatch["actual_snapshot"]["state"]["time_s"], "event": "independent_dispatch_admission",
        "kind": "recorded_rules_gate", "source": {"record": "mission", "pointer": "/governance/dispatch"}})
    _require(all(_number(row["time_s"]) for row in timeline), "restored_replay_nonfinite_timeline")
    _require(execution["mode"] != "fixture" or type(verified.get("confirmed_native_inferences")) is int
        and verified["confirmed_native_inferences"] == 0 and verified.get("ai_control_completed") is False,
        "restored_replay_fixture_borrowed_native_inference_claim")
    review = _review_projection(mission, host, verified)
    metric_keys = ("passed", "physical_success", "ship_physical_success", "booster_physical_success", "ship_contact", "booster",
        "integration_policy", "judgment_chain_verified", "honored_native_return_choice", "operator_amendment_consistency_verified",
        "hosted_model_attempts", "confirmed_native_inferences", "ai_control_completed", "all_features_completed",
        "retained_count", "payload_released_count", "return_candidate_id", "reservation_change_count", "termination", "return_review")
    return {"schema": SCHEMA, "case_id": execution["case_id"], "source_run_id": execution["context"]["run_id"],
        "mode": execution["mode"], "clock": {"type": "absolute_mission_simulation", "unit": "s", "origin_time_s": 0.,
            "start_time_s": ship["start_time_s"], "end_time_s": ship["end_time_s"], "wall_clock_not_retimed_as_simulation": True},
        "coordinates": {"position": "ECI", "position_unit": "m", "velocity_unit": "m/s", "angular_rate_unit": "rad/s",
            "quaternion": "scalar-first Hamilton body-to-ECI", "body_axis": "+Z", "earth_projection_requires_bound_profile": True},
        "bodies": bodies, "release_receipts": releases, "timeline": sorted(timeline, key=lambda row: row["time_s"]),
        "judgments": _judgments(host, review), "verified_metrics": {key: deepcopy(verified[key]) for key in metric_keys if key in verified},
        "provenance": {"mission_raw_sha256": expected_mission_sha256, "host_raw_sha256": expected_host_sha256,
            "verification_raw_sha256": expected_verification_sha256, "plan_sha256": execution["plan_sha256"],
            "source_manifest_sha256": _digest(execution["source_sha256"]), "profile_sha256": execution["profile_sha256"],
            "external_hash_pairing_is_not_signature_authentication": True},
        "display_policy": {"autoplay": False, "loop": False, "recorded_points_only": True,
            "predictions_included": False, "new_satellite_propagation": False, "new_physical_states": False,
            "metrics_recomputed_from_display": False, "physical_verifier_reexecuted": False,
            "internal_reasoning_invented": False, "render_or_video_produced": False}}

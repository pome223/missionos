"""Independent saved-record checks for the opt-in visual extension.

No simulator, tracker, detector, actor, renderer or model is run here. Scalar
checks bind source-time counter evidence, physical release events, commands,
pixels and the existing independent return verifier. This is record integrity,
not independent spacecraft physics or validation of a model's interpretation.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path

from .starship_visual_contract import digest, finite, validate_authority, validate_request
from .starship_deployment_handoff_verifier import (
    STATE, _history, _observed_origin, _state, _vehicle_inventory,
)


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _counter_evidence(record, launch):
    receipts, attempts = record["counter_receipts"], record["attempts"]
    releases = [e for e in launch["events"] if e["event"] == "payload_released"]
    final = launch["final_state"]["time_s"]
    by_id = {}
    previous_time = -1.
    attempt_events = [e for e in launch["events"] if e["event"] == "visual_release_attempt_observed"]
    require(len(attempts) == len(attempt_events), "visual_attempt_execution_count")
    for attempt, executed in zip(attempts, attempt_events):
        token, when = attempt["release_attempt_id"], attempt["source_time_s"]
        require(token not in by_id and finite(when) and previous_time < when <= final,
                "visual_attempt_identity_or_order")
        require(type(attempt["acknowledged"]) is bool and type(attempt["expected_count"]) is int,
                "visual_attempt_types")
        require(all(executed[k] == v for k, v in attempt.items()) and executed["time_s"] == when,
                "visual_attempt_execution_binding")
        before = sum(e["time_s"] < when for e in releases)
        require(attempt["expected_count"] == before + 1, "visual_attempt_expected_count")
        by_id[token], previous_time = attempt, when
    previous, sequence, count = -1., 0, 0
    for receipt in receipts:
        source, available = receipt["source_time_s"], receipt["available_time_s"]
        require(receipt["schema"] == "missionos.starship_counter_receipt.v1"
                and receipt["run_id"] == record["run_id"]
                and receipt["receipt_sha256"] == digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}),
                "visual_counter_identity")
        require(all(finite(v) for v in (source, available)) and 0 <= source <= available <= final + 1e-8
                and source >= previous and type(receipt["source_sequence"]) is int
                and receipt["source_sequence"] == sequence + 1, "visual_counter_source_order_or_gap")
        observed = receipt["confirmed_count"]
        require(type(observed) is int and count <= observed <= count + 1,
                "visual_counter_count_order")
        # A source sampled before the release on the same plant tick can still
        # report the previous count. Either value must match actual events.
        low = sum(e["time_s"] < source for e in releases)
        high = sum(e["time_s"] <= source for e in releases)
        require(low <= observed <= high, "visual_counter_not_backed_by_release")
        token = receipt["release_attempt_id"]
        if token is not None:
            require(token in by_id and by_id[token]["source_time_s"] <= source,
                    "visual_counter_attempt_binding")
        previous, sequence, count = source, receipt["source_sequence"], observed
    require(count == record["observed_count"], "visual_observed_inventory")
    return receipts, by_id, releases


def _latest(receipts, now):
    arrived = [r for r in receipts if r["available_time_s"] <= now + 1e-8]
    return arrived[-1] if arrived else None


def _fresh(receipts, attempts, now, *, increment=False):
    row = _latest(receipts, now)
    relevant = [a for a in attempts.values() if a["source_time_s"] <= now]
    last = relevant[-1] if relevant else None
    return bool(row and 0 <= now-row["source_time_s"] <= 2.001 + 1e-8
                and (last is None or (row["release_attempt_id"] == last["release_attempt_id"]
                     and row["source_time_s"] >= last["source_time_s"]
                     and (not increment or row["confirmed_count"] == last["expected_count"]))))


def _visual_chain(authority, record, launch):
    require(record["schema"] == "missionos.starship_visual_record.v1"
            and record["authority"] == authority and record["authority_sha256"] == digest(authority),
            "visual_record_authority")
    require(all(record[key] is False for key in ("hosted_model_attempted", "model_inference_invoked",
                                               "physical_execution", "automatic_retry")), "visual_record_claims")
    cfg, episode = authority["visual"], record["episode"]
    requests, results, commands = record["requests"], record["results"], record["commands"]
    require(len(requests) <= 2 and len(record["observation_requests"]) <= 1,
            "visual_episode_budget")
    receipts, attempts, releases = _counter_evidence(record, launch)
    if episode is None:
        require(not requests and not commands and not record["events"], "visual_no_episode_activity")
        return receipts, attempts, releases
    event = episode["event"]
    require(record["events"] == [event] and event["schema"] == "missionos.starship_anomaly_event.v1"
            and event["authority_sha256"] == digest(authority) and event["run_id"] == record["run_id"]
            and event["vehicle_id"] == "ship" and event["episode_id"] == episode["episode_id"],
            "visual_anomaly_binding")
    attempt = attempts[event["release_attempt_id"]]
    detected = event["detected_at_sim_s"]
    received = _latest(receipts, detected)
    require(event["metric_id"] == "release_confirmation_timeout"
            and event["detector_version"] == "arrival_confirmation_v1"
            and event["observed_value"] == (received["confirmed_count"] if received else None)
            and event["expected_band"] == [attempt["expected_count"]] * 2
            and event["unit"] == "payload_count"
            and event["source_window_start_s"] == attempt["source_time_s"]
            and event["source_window_end_s"] == detected, "visual_anomaly_metric_fields")
    require(detected >= attempt["source_time_s"] + cfg["confirmation_timeout_s"]
            and not (received and received["release_attempt_id"] == attempt["release_attempt_id"]
                     and received["confirmed_count"] == attempt["expected_count"]),
            "visual_anomaly_without_numeric_basis")
    require(event["telemetry_evidence_sha256"] == digest({"attempt": attempt, "receipt": received})
            and event["detector_config_sha256"] == digest({"confirmation_timeout_s": cfg["confirmation_timeout_s"]}),
            "visual_anomaly_evidence_hash")
    deadline = episode["deadline_s"]
    require(event["episode_deadline_sim_s"] == deadline and episode["opened_at_s"] == detected
            and detected < deadline <= detected + cfg["hold_s"]
            and episode["wall_budget_s"] == deadline - detected, "visual_episode_deadline")
    by_request, previous_snapshot = {}, -1.
    for request in requests:
        validate_request(request)
        require(request["run_id"] == record["run_id"] and request["request_id"] not in by_request
                and request["episode_id"] == episode["episode_id"]
                and request["release_attempt_id"] == attempt["release_attempt_id"]
                and request["event_id"] == event["event_id"]
                and request["authority_sha256"] == digest(authority)
                and request["deadline_s"] == deadline
                and detected <= request["snapshot_time_s"] < deadline
                and request["snapshot_time_s"] > previous_snapshot, "visual_request_binding")
        from .starship_visual_observation import validate_evidence
        validate_evidence(request["manifest"], now=request["snapshot_time_s"], run_id=record["run_id"],
                          vehicle_id="ship", max_age_s=cfg["frame_max_age_s"])
        if request["evidence_kind"] == "visual":
            require(any(f["capture_sim_time_s"] > attempt["source_time_s"]
                        for f in request["manifest"]["frames"]), "visual_no_post_attempt_frame")
        current = _latest(receipts, request["snapshot_time_s"])
        telemetry = request["telemetry"]
        require(telemetry["last_confirmed_count"] == (current["confirmed_count"] if current else 0)
                and telemetry["counter_source_time_s"] == (current["source_time_s"] if current else None)
                and telemetry["counter_available_time_s"] == (current["available_time_s"] if current else None)
                and telemetry["counter_source_sequence"] == (current["source_sequence"] if current else None)
                and telemetry["release_attempt_id"] == attempt["release_attempt_id"]
                and telemetry["expected_count"] == attempt["expected_count"]
                and telemetry["inventory_fresh"] is _fresh(receipts, attempts, request["snapshot_time_s"])
                and telemetry["release_confirmation_valid"] is _fresh(receipts, attempts, request["snapshot_time_s"], increment=True),
                "visual_request_counter_binding")
        if len(by_request):
            require(len(record["observation_requests"]) == 1, "visual_followup_without_observation")
            first_result = next((r for r in results if r["request_id"] == requests[0]["request_id"] and r["accepted"]), None)
            require(first_result is not None and request["prior_diagnosis"] == first_result["response"]["visual_diagnosis"],
                    "visual_followup_prior_diagnosis_binding")
        else:
            require(request["prior_diagnosis"] is None, "visual_initial_prior_diagnosis")
        if "continue" in request["allowed_actions"]:
            require(_fresh(receipts, attempts, request["snapshot_time_s"], increment=True),
                    "visual_continue_without_fresh_counter")
        expected_actions, expected_choices = ["stop_deployment"], ["none"]
        if (telemetry["sequencer_state"] in {"running", "held"}
                and _fresh(receipts, attempts, request["snapshot_time_s"], increment=True)
                and telemetry["orbit_release_feasible"] is True
                and finite(telemetry["fuel_kg"])
                and telemetry["fuel_kg"] - 100. >= authority["m2_authority"]["deployment_authority"]["minimum_return_fuel_kg"]):
            expected_actions.insert(0, "continue")
        if not by_request:
            expected_actions += ["request_visual_observation", "request_numeric_confirmation"]
            expected_choices += ["alternate_camera", "next_frame_primary", "fresh_separation_counter"]
        require(request["allowed_actions"] == expected_actions
                and request["observation_choices"] == expected_choices, "visual_independent_action_choices")
        by_request[request["request_id"]], previous_snapshot = request, request["snapshot_time_s"]
    seen_results = set()
    for result in results:
        request = by_request[result["request_id"]]
        require(result["request_id"] not in seen_results, "visual_duplicate_result")
        seen_results.add(result["request_id"])
        require(request["snapshot_time_s"] < result["received_at_s"] <= deadline, "visual_result_time")
        answer = result["response"]
        if result["accepted"]:
            require(result["received_at_s"] + cfg["apply_margin_s"] < deadline, "visual_result_application_margin")
            require(result["current_state_generation"] == request["state_generation"], "visual_result_state_generation")
            require(finite(result["received_wall_elapsed_s"])
                    and 0 <= result["received_wall_elapsed_s"] < episode["wall_budget_s"] - cfg["apply_margin_s"],
                    "visual_result_wall_application_margin")
            require(result["rejection"] is None and answer["status"] == "fixture_valid"
                    and answer["request_sha256"] == digest(request)
                    and answer["request_id"] == request["request_id"]
                    and answer["episode_id"] == request["episode_id"]
                    and answer["run_id"] == record["run_id"] and answer["model_inference_invoked"] is False
                    and answer["action"] in request["allowed_actions"]
                    and answer["observation_choice"] in request["observation_choices"], "visual_accepted_response_binding")
            diagnosis = answer["visual_diagnosis"]
            require(diagnosis["diagnosis_id"] == digest({k:v for k,v in diagnosis.items() if k != "diagnosis_id"})
                    and diagnosis["request_sha256"] == digest(request)
                    and diagnosis["request_id"] == request["request_id"]
                    and diagnosis["episode_id"] == request["episode_id"]
                    and diagnosis["evidence_kind"] == request["evidence_kind"]
                    and diagnosis["snapshot_time_s"] == request["snapshot_time_s"]
                    and diagnosis["manifest_sha256"] == digest(request["manifest"])
                    and diagnosis["recommended_action"] == answer["action"]
                    and diagnosis["observation_choice"] == answer["observation_choice"]
                    and diagnosis["model_id"] == "jev-1.14.0", "visual_diagnosis_binding")
            allowed_questions = {
                "telemetry_relation": {"consistent", "conflicting", "insufficient"},
                "recommended_action": set(request["allowed_actions"]),
                "observation_choice": set(request["observation_choices"]),
            }
            if request["evidence_kind"] == "visual":
                allowed_questions.update(visibility={"adequate", "occluded", "off_camera", "ambiguous"},
                    deployment_visual_state={"separation_visible", "suspected_retention", "indeterminate"})
                allowed_questions.update({f"frame_{i}_support": {"supports", "not_supports", "uncertain"}
                                          for i in range(len(request["manifest"]["frames"]))})
                require(set(diagnosis["evidence_frame_ids"]) <= {f["frame_id"] for f in request["manifest"]["frames"]},
                        "visual_diagnosis_frame_binding")
                if diagnosis["deployment_visual_state"] != "indeterminate":
                    require(bool(diagnosis["evidence_frame_ids"]), "visual_finding_without_support")
                if diagnosis["deployment_visual_state"] == "separation_visible":
                    require(diagnosis["visibility"] == "adequate", "visual_finding_without_visibility")
            probabilities, confidence = diagnosis["probabilities"], diagnosis["confidence"]
            require(set(probabilities) == set(confidence) == set(allowed_questions), "visual_diagnosis_questions")
            for key, options in allowed_questions.items():
                values = probabilities[key]
                require(set(values) == options and all(finite(v) and 0 <= v <= 1 for v in values.values())
                        and math.isclose(sum(values.values()), 1.0, abs_tol=.001)
                        and finite(confidence[key]) and 0 <= confidence[key] <= 1, "visual_diagnosis_probabilities")
                if key in diagnosis:
                    require(diagnosis[key] in options, "visual_diagnosis_enum")
            if request["evidence_kind"] == "numeric_followup":
                require(not {"visibility", "deployment_visual_state", "evidence_frame_ids"} & set(diagnosis),
                        "numeric_followup_invented_image")
                require(diagnosis["prior_diagnosis_id"] == request["prior_diagnosis"]["diagnosis_id"],
                        "visual_numeric_prior_diagnosis")
            receipt = answer["provider_receipt"]
            require(receipt["status"] == "fixture_succeeded" and receipt["transport_mode"] == "loopback_fixture"
                    and receipt["local_http_attempted"] is True
                    and receipt["hosted_model_attempted"] is receipt["model_inference_invoked"] is receipt["call_attempted"] is False
                    and receipt["call_succeeded"] is False
                    and receipt["response_received"] is receipt["complete_response_observed"] is True
                    and receipt["model_id"] == receipt["requested_model_id"] == "jev-1.14.0"
                    and receipt["semantic_request_sha256"] == digest(request)
                    and receipt["retries"] == 0 and 3 <= receipt["timeout_seconds"] <= 8
                    and finite(receipt["remaining_budget_s"])
                    and receipt["timeout_seconds"] == min(8., receipt["remaining_budget_s"] - cfg["apply_margin_s"])
                    and receipt["remaining_budget_s"] <= deadline - request["snapshot_time_s"] + 1e-8
                    and receipt["request_bytes"] <= cfg["maximum_wire_bytes"], "visual_fixture_receipt")
    observation_requests, observation_results = record["observation_requests"], record["observation_results"]
    for operation in observation_requests:
        first = by_request[operation["request_id"]]
        accepted = [r for r in results if r["request_id"] == first["request_id"] and r["accepted"]]
        require(len(accepted) == 1 and operation["episode_id"] == episode["episode_id"]
                and operation["choice"] == accepted[0]["response"]["observation_choice"]
                and operation["requested_at_s"] == accepted[0]["received_at_s"]
                and operation["deadline_s"] == deadline
                and accepted[0]["response"]["action"] in {"request_visual_observation", "request_numeric_confirmation"},
                "visual_observation_request_binding")
        completions = [r for r in observation_results if r["requested_at_s"] == operation["requested_at_s"]]
        require(len(completions) <= 1, "visual_duplicate_observation_result")
        followups = [r for r in requests if r["snapshot_time_s"] > operation["requested_at_s"]]
        if followups:
            require(len(followups) == len(completions) == 1 and operation["status"] == "completed",
                    "visual_followup_completion_missing")
            followup, complete = followups[0], completions[0]
            require(operation["requested_at_s"] < complete["completed_at_s"] == followup["snapshot_time_s"] < deadline
                    and complete["episode_id"] == episode["episode_id"]
                    and complete["evidence_kind"] == followup["evidence_kind"]
                    and complete["status"] == "available", "visual_observation_result_time")
            if operation["choice"] == "fresh_separation_counter":
                require(followup["evidence_kind"] == "numeric_followup"
                        and followup["telemetry"]["counter_source_time_s"] > operation["requested_at_s"],
                        "visual_numeric_observation_not_new")
            else:
                camera = "deployment_port_secondary" if operation["choice"] == "alternate_camera" else "deployment_port_primary"
                require(followup["evidence_kind"] == "visual" and bool(followup["manifest"]["frames"])
                        and all(f["camera_id"] == camera and f["capture_sim_time_s"] > operation["requested_at_s"]
                                for f in followup["manifest"]["frames"]), "visual_camera_observation_not_new")
        else:
            require(len(completions) == 1 and operation["status"] == "expired",
                    "visual_unused_observation_completion")
            complete = completions[0]
            expired = [c for c in commands if c["action"] == "stop_deployment"
                       and c["origin"] == "fallback"
                       and c["time_s"] == complete["completed_at_s"]]
            expected_kind = ("numeric_followup" if operation["choice"] == "fresh_separation_counter"
                             else "visual")
            require(complete["schema"] == "missionos.starship_visual_observation_result.v1"
                    and complete["episode_id"] == episode["episode_id"]
                    and complete["status"] == "expired" and complete["evidence_kind"] == expected_kind
                    and operation["requested_at_s"] < complete["completed_at_s"]
                    and len(expired) == 1, "visual_observation_expiry_binding")
            stop = expired[0]
            require((stop["reason"] == "original_episode_deadline" and stop["time_s"] >= deadline)
                    or (stop["reason"] == "original_episode_wall_deadline"
                        and finite(stop["wall_elapsed_s"]) and stop["wall_elapsed_s"] >= episode["wall_budget_s"]),
                    "visual_observation_expired_before_deadline")
    actual_events = [e for e in launch["events"] if e["event"] == "visual_mission_command"]
    require(len(actual_events) == len(commands), "visual_command_event_count")
    for command, executed in zip(commands, actual_events):
        require(all(command[k] == executed[k] for k in ("time_s", "episode_id", "action", "origin", "reason", "authority_sha256")),
                "visual_dispatch_event_binding")
        before, after = command["observation"], command["later_observation"]
        require(after is not None and before["time_s"] == command["time_s"] < after["time_s"],
                "visual_command_later_observation")
        action, when = command["action"], command["time_s"]
        require(command["deadline_s"] == deadline and command["authority_sha256"] == digest(authority),
                "visual_dispatch_deadline_binding")
        if action == "hold":
            require(command["origin"] == "rules" and before["sequencer_state"] == "running"
                    and after["sequencer_state"] == "held" and when == detected, "visual_hold_origin_or_transition")
        elif action == "continue":
            require(command["origin"] == "fixture_judgment" and before["sequencer_state"] == "held"
                    and after["sequencer_state"] == "running" and when < deadline
                    and _fresh(receipts, attempts, when, increment=True)
                    and before["numerical_tools"]["orbit_release_feasible"] is True,
                    "visual_resume_conditions")
        else:
            require(action == "stop_deployment" and after["sequencer_state"] in {"skipped", "inhibited"},
                    "visual_stop_transition")
            if command["origin"] == "fallback":
                require(when >= deadline or command["reason"] == "original_episode_wall_deadline"
                        and finite(command["wall_elapsed_s"]) and command["wall_elapsed_s"] >= episode["wall_budget_s"],
                        "visual_fallback_before_deadline")
        if command["origin"] == "fixture_judgment":
            matching = [r for r in results if r["request_id"] == command["request_id"] and r["accepted"]
                        and r["response"]["action"] == action and r["received_at_s"] == when]
            require(len(matching) == 1, "visual_command_without_accepted_judgment")
            require(command["state_generation"] == by_request[command["request_id"]]["state_generation"],
                    "visual_command_state_generation")
    return receipts, attempts, releases


def _visual_history(record, history):
    """Bind observed inventory and actor generations without replaying an actor."""
    receipts = record["counter_receipts"]
    generation, previous, generations, command_generations = 0, None, [], {}
    for row in history:
        if row["kind"] == "command":
            command_generations[(row["time_s"], row["action"])] = generation
            generation += 1
        elif row["kind"] == "observation":
            current = _latest(receipts, row["time_s"])
            require(row["released_count"] == (current["confirmed_count"] if current else 0),
                    "visual_tracker_not_arrived_counter")
            if previous is not None and any(row[k] != previous[k] for k in ("released_count", "sequencer_state")):
                generation += 1
            previous = row
        else:
            raise ValueError("visual_unknown_handoff_history")
        generations.append((row["time_s"], generation))
    for request in record["requests"]:
        prior = [g for t, g in generations if t <= request["snapshot_time_s"]]
        require(prior and request["state_generation"] == prior[-1], "visual_request_state_generation")
    for command in record["commands"]:
        require(command["state_generation"] == command_generations[(command["time_s"], command["action"])],
                "visual_dispatch_state_generation")
        if command["action"] == "continue":
            fuel = command["observation"]["fuel_kg"]
            minimum = record["authority"]["m2_authority"]["deployment_authority"]["minimum_return_fuel_kg"]
            require(finite(fuel) and fuel-100. >= minimum, "visual_continue_fuel_margin")
        if command["origin"] == "fixture_judgment":
            request = next(r for r in record["requests"] if r["request_id"] == command["request_id"])
            require(request["state_generation"] == command["state_generation"], "visual_stale_generation_dispatch")
    return generation


def _visual_trace(record, trace, total_count):
    """Check raw extension identity before projecting onto historical rows."""
    require(type(trace) is list and len(trace) <= 50000
            and type(total_count) is int and 1 <= total_count <= 26, "visual_trace_shape")
    raw, projected, observations = [], [], []
    for row in trace:
        require(type(row) is dict, "visual_trace_entry")
        if row.get("kind") == "visual_command":
            raw.append(row)
            projected.append({"kind": "command", "action": row["action"], "time_s": row["time_s"]})
        else:
            projected.append(deepcopy(row))
            if row.get("kind") == "observation":
                observations.append(row["row"])
    expected = [{"kind": "visual_command", **{key: command[key] for key in
                 ("action", "time_s", "episode_id", "origin")}} for command in record["commands"]]
    # Equality deliberately includes the exact field set, sequence and count.
    # A forged episode/origin must not disappear during legacy projection.
    require(raw == expected, "visual_raw_command_trace_binding")
    for command in record["commands"]:
        before = [row for row in observations if row["time_s"] == command["time_s"]]
        later = [row for row in observations if row["time_s"] > command["time_s"]]
        require(command["observation"] in before and later
                and command["later_observation"] == later[0], "visual_command_trace_observation_binding")
    for request in record["requests"]:
        snapshots = [row for row in observations if row["time_s"] == request["snapshot_time_s"]]
        require(bool(snapshots), "visual_request_trace_snapshot_missing")
        observed, telemetry = snapshots[-1], request["telemetry"]
        require(all(telemetry[key] == observed[key] for key in
                    ("time_s", "phase", "sequencer_state", "release_acknowledged", "fuel_kg", "return_deadline_s"))
                and telemetry["last_confirmed_count"] == observed["released_count"]
                and telemetry["orbit_release_feasible"] == observed["numerical_tools"]["orbit_release_feasible"],
                "visual_request_trace_telemetry_binding")
    history, releases, latest, generation = _history(projected, total_count)
    require(generation == _visual_history(record, history), "visual_trace_generation")
    return history, releases, latest, generation


def _closed_visual_episode(record, now):
    episode = record["episode"]
    if episode is None:
        return
    require(episode["status"] == "closed" and record["commands"], "visual_open_episode_handoff")
    last = record["commands"][-1]
    require(last["action"] in {"continue", "stop_deployment"}
            and episode["closure_action"] == last["action"]
            and episode["closure_origin"] == last["origin"]
            and episode["closed_at_s"] == last["time_s"] <= now, "visual_episode_closure_binding")
    require({row["request_id"] for row in record["requests"]}
            == {row["request_id"] for row in record["results"]}
            and all(row["status"] in {"completed", "expired"} for row in record["observation_requests"]),
            "visual_pending_operation_at_closure")


def _verify_launch_record(profile, launch):
    """Unresolved inventory still requires a scalar-verified flown prefix."""
    from .starship_sixdof_verifier import verify_study
    checked = verify_study({"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
        "runs": [launch], "provenance": {"physical_execution_invoked": False,
                                         "starship_vehicle_validated": False}}, expected_scenario="launch")
    require(checked["passed"], "visual_unresolved_launch_trajectory_verification")
    return checked


def _validate_fixture_response(request, result, observed, payload):
    """Compare native HTTP answers and derived claims without the agent decoder."""
    native = observed["native_response"]
    body = json.dumps(native, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")
    require(0 < len(body) <= 65536 and observed["response_bytes"] == len(body)
            and observed["response_sha256"] == sha256(body).hexdigest(),
            "visual_fixture_native_response_bytes")
    response = result["response"]
    receipt, diagnosis = response["provider_receipt"], response["visual_diagnosis"]
    if receipt["http_status"] is not None:
        require(receipt["http_status"] == observed["response_http_status"], "visual_fixture_response_http_status")
    if receipt["complete_response_observed"]:
        require(receipt["response_sha256"] == observed["response_sha256"]
                and receipt["response_bytes_read"] == len(body), "visual_fixture_response_receipt_binding")
    if diagnosis is None:
        require(not result["accepted"] and receipt["status"] != "fixture_succeeded",
                "visual_fixture_missing_diagnosis")
        return
    require(observed["response_http_status"] == 200 and receipt["complete_response_observed"] is True
            and type(native) is dict and set(native) == {"model", "answers", "usage"}
            and native["model"] == "jev-1.14.0" and type(native["answers"]) is dict
            and set(native["answers"]) == set(payload["questions"]), "visual_fixture_native_response_shape")
    require(diagnosis["question_contract_sha256"] == digest(payload["questions"])
            == receipt["question_contract_sha256"], "visual_diagnosis_question_contract")
    chosen = {}
    for key, question in payload["questions"].items():
        answer, options = native["answers"][key], set(question["criteria"])
        require(type(answer) is dict and set(answer) == {"type", "choice", "probabilities", "confidence"}
                and answer["type"] == "choice" and answer["choice"] in options
                and type(answer["probabilities"]) is dict and set(answer["probabilities"]) == options,
                "visual_fixture_native_choice_shape")
        probabilities, confidence = answer["probabilities"], answer["confidence"]
        require(all(finite(value) and 0 <= value <= 1 for value in probabilities.values())
                and math.isclose(sum(probabilities.values()), 1., abs_tol=.001)
                and finite(confidence) and 0 <= confidence <= 1,
                "visual_fixture_native_distribution")
        require(digest(diagnosis["probabilities"][key]) == digest(probabilities)
                and diagnosis["confidence"][key] == confidence,
                "visual_diagnosis_native_distribution_binding")
        chosen[key] = answer["choice"]
        if not key.startswith("frame_"):
            require(diagnosis[key] == answer["choice"], "visual_diagnosis_native_choice_binding")
    require(response["action"] == chosen["recommended_action"]
            and response["observation_choice"] == chosen["observation_choice"]
            and diagnosis["confidence_calibrated"] is False,
            "visual_response_native_action_binding")
    if request["evidence_kind"] == "visual":
        supported = [frame["frame_id"] for index, frame in enumerate(request["manifest"]["frames"])
                     if chosen[f"frame_{index}_support"] == "supports"]
        require(diagnosis["evidence_frame_ids"] == supported, "visual_diagnosis_native_frame_support")
    else:
        require(not {"visibility", "deployment_visual_state", "evidence_frame_ids"} & set(diagnosis)
                and diagnosis["prior_diagnosis_id"] == request["prior_diagnosis"]["diagnosis_id"],
                "visual_numeric_native_scope")


def _visual_completion_issues(case, handoff, returning):
    """A valid safe-stop record is distinct from a completed deployment case."""
    issues = []
    if case in {"normal", "delayed_confirmation", "primary_occluded"}:
        if handoff["released_count"] != handoff["total_count"]:
            issues.append("complete_deployment_not_observed")
    if case == "normal":
        dispatch = returning["dispatch"]
        if (dispatch is None or dispatch["candidate_id"] != "nominal"
                or any(row["action"] != "continue" for row in handoff["history"] if row["kind"] == "command")):
            issues.append("normal_plan_unnecessarily_changed")
    return issues


def _visual_case_acceptance(case, handoff, returning, checked):
    """Apply visual case semantics after independent return verification.

    The inner event_normal case selects notice semantics. Its nominal-only
    completion requirement is not the visual retained-payload return contract.
    This scalar composition neither admits a candidate nor changes its verdict.
    """
    issues = _visual_completion_issues(case, handoff, returning)
    if checked["outcome"] != "contact_and_recovery_area_met":
        issues.insert(0, "contact_and_area_not_met")
    return {"case_accepted": checked["passed"] is True and not issues,
            "case_issues": issues}


def _validate_m2_bindings(study, expected_sources, expected_run_id):
    """Keep the visual extension inside the original frozen M2 input scope."""
    from .starship_replanning import source_hashes as return_sources

    authority, m2 = study["authority"]["m2_authority"], study["m2"]
    # Use the registered source names, but take every expected hash from the
    # independently supplied preflight manifest, never from the saved study.
    expected_return = {name: expected_sources[name] for name in return_sources()}
    require(bool(expected_return) and study["return_source_sha256"] == expected_return
            and m2["return_source_sha256"] == expected_return, "visual_return_source_manifest_binding")
    require(m2["schema"] == "missionos.starship_m2_study.v1" and m2["run_id"] == expected_run_id,
            "visual_m2_identity")
    require(digest(study["profile"]) == digest(m2["profile"]) == authority["profile_sha256"],
            "visual_authorized_profile_binding")
    returning = m2["return_study"]
    if returning is not None:
        require(digest(returning["profile"]) == authority["profile_sha256"],
                "visual_return_profile_binding")
        require("runtime_failure" not in returning, "visual_return_runtime_failure")
    expected_budget = {"checkpoint_time_s": authority["checkpoint_time_s"],
                       "maximum_model_calls": authority["maximum_model_calls"], "maximum_attempts": 1,
                       **{key: authority["return_authority"][key] for key in
                          ("maximum_forecasts", "maximum_workers", "forecast_timeout_s",
                           "batch_timeout_s", "maximum_delay_s")}}
    require(m2["budget"] == expected_budget, "visual_m2_execution_budget")
    runtime = m2["runtime"]
    require(runtime["schema"] == "missionos.starship_m2_runtime.v1"
            and runtime["mode"] == "fixture" and runtime["boundary"] == "local_cli_fixture_continuation"
            and runtime["deployment_started"] is runtime["sources_unchanged"] is True
            and runtime["continuation_started"] is (returning is not None)
            and runtime["retry_performed"] is runtime["physical_execution"] is False
            and type(runtime["model_calls"]) is type(runtime["human_inflight_commands"]) is int
            and runtime["model_calls"] == runtime["human_inflight_commands"] == 0
            and finite(runtime["wall_time_s"]) and runtime["wall_time_s"] >= 0,
            "visual_m2_runtime_scope")
    if m2["handoff"] is None:
        require(study["authority"]["case"] == "unknown_inventory" and returning is None
                and m2.get("runtime_failure", {}).get("message") == "unresolved_inventory",
                "visual_m2_unresolved_scope")
    else:
        require(returning is not None and "runtime_failure" not in m2, "visual_m2_runtime_failure")
    from .starship_m2_verifier import _no_model
    require(_no_model(m2["deployment_run"]["mission_director"]["records"])
            and (returning is None or _no_model(returning["decisions"])),
            "visual_unexpected_legacy_model_invocation")
    if returning is None:
        from .starship_m2_decision_verifier import verify_decisions
        # The confirmed no-continuation branch has no return decisions. This
        # shallow validator-only projection reuses the deployment Rules audit;
        # it is never saved or presented as a return study/executed return.
        verify_decisions({**m2, "return_study": {"decisions": []}})
    return expected_return


def validate_visual_origin(authority, inner, handoff, launch, *, trace=None):
    validate_authority(authority)
    require(authority["m2_authority"] == inner, "visual_inner_authority")
    record = launch["visual_observation"]
    receipts, attempts, releases = _visual_chain(authority, record, launch)
    now = launch["final_state"]["time_s"]
    require(_fresh(receipts, attempts, now), "visual_inventory_not_fresh_at_handoff")
    require(handoff["released_count"] == record["observed_count"] == len(releases)
            and handoff["retained_count"] == handoff["total_count"]-len(releases), "visual_confirmed_handoff_inventory")
    _closed_visual_episode(record, now)
    if record["commands"]:
        require(receipts[-1]["source_time_s"] > record["commands"][-1]["time_s"], "visual_handoff_before_final_confirmation")
    require(handoff["generation"] == _visual_history(record, handoff["history"]), "visual_handoff_generation")
    if trace is not None:
        history, confirmed, latest, generation = _visual_trace(record, trace, handoff["total_count"])
        require(history == handoff["history"] and confirmed == handoff["release_observations"]
                and generation == handoff["generation"] and latest["time_s"] == now, "visual_tracker_history")
    commands = [{"action": e["action"], "time_s": e["time_s"]} for e in launch["events"]
                if e["event"] == "visual_mission_command"
                or e["event"] == "managed_mission_command" and e["point"].startswith("deployment")]
    require(commands == [{k: r[k] for k in ("action", "time_s")} for r in handoff["history"] if r["kind"] == "command"],
            "visual_handoff_command_history")
    return True


def _validate_visual_sequence_evidence(launch, trace, authority):
    """Replay scalar sequence transitions, including the independent interlock.

    Visual commands have their own event identity and do not emit the legacy
    ``managed_deployment_resumed`` event. No producer or dynamics are called.
    Same-tick observations precede commands/interlocks, but follow hold expiry.
    """
    require(not any(e["event"] == "supervised_deployment_withheld" for e in launch["events"]),
            "visual_m3_prefix_outside_scope")
    director = launch["mission_director"]
    require(digest(director["envelope"]) == digest(authority["deployment_authority"])
            and director["response_fault"] == authority["deployment_response_fault"]
            and launch["mission_management_case"] == authority["deployment_case"],
            "visual_deployment_grant_binding")
    rows = [entry["row"] for entry in trace if entry["kind"] == "observation"]
    require(bool(rows), "visual_sequence_observation_missing")
    observed = {digest(row) for row in rows}
    events = [event for event in launch["events"] if event["event"] in {
        "managed_hold_expired", "managed_deployment_resumed", "deployment_interlock_inhibited",
        "visual_mission_command"}
        or event["event"] == "managed_mission_command" and event["point"].startswith("deployment")]
    require(all(finite(e["time_s"]) and 0 <= e["time_s"] <= rows[-1]["time_s"] for e in events)
            and all(a["time_s"] <= b["time_s"] for a, b in zip(events, events[1:])),
            "visual_sequencer_event_chronology")
    timeline = [(e["time_s"], 0 if e["event"] == "managed_hold_expired" else 2, i, "event", e)
                for i, e in enumerate(events)]
    timeline += [(row["time_s"], 1, i, "observation", row) for i, row in enumerate(rows)]
    sequence, interlock, last_resume = "running", False, None
    expected_resumes, observed_resumes = [], []
    for now, _, _, kind, entry in sorted(timeline):
        if kind == "observation":
            require(entry["sequencer_state"] == sequence, "visual_sequence_not_backed_by_core_events")
            continue
        event = entry["event"]
        if event == "managed_hold_expired":
            require(sequence == "held" and not interlock, "visual_hold_expiry_without_held_sequence")
            sequence = "skipped"
        elif event == "deployment_interlock_inhibited":
            interlock, sequence = True, "inhibited"
        elif event == "managed_deployment_resumed":
            require(not interlock and sequence == "running" and last_resume == now,
                    "visual_legacy_resume_without_continue")
            observed_resumes.append(now)
        else:
            action, visual = entry["action"], event == "visual_mission_command"
            require(action in ({"hold", "continue", "stop_deployment"} if visual else
                               {"hold", "continue", "stop_deployment", "collect_status"}),
                    "visual_unsupported_core_command")
            if action == "hold":
                require(not interlock and (not visual or sequence == "running"),
                        "visual_hold_overwrites_interlock_or_closed_sequence")
                sequence = "held"
            elif action == "stop_deployment":
                sequence = "skipped"
            elif action == "collect_status" and sequence not in {"skipped", "inhibited"}:
                require(not interlock, "visual_status_overwrites_interlock")
                sequence = "held"
            elif action == "continue":
                require(not visual or sequence == "held" and not interlock,
                        "visual_continue_without_uninhibited_hold")
                if sequence == "held":
                    require(not interlock, "visual_continue_overwrites_interlock")
                    sequence = "running"
                    if not visual:
                        last_resume = now
                        expected_resumes.append(now)
    require(sequence == rows[-1]["sequencer_state"], "visual_unobserved_final_sequence_transition")
    require(expected_resumes == observed_resumes, "visual_legacy_resume_event_missing_or_duplicated")
    executed = [{k: e[k] for k in ("point", "action", "time_s")}
                for e in events if e["event"] == "managed_mission_command"]
    require([{k: row[k] for k in ("action", "time_s")} for row in trace if row["kind"] == "command"]
            == [{k: e[k] for k in ("action", "time_s")} for e in executed],
            "visual_legacy_command_trace_binding")
    dispatched = []
    for record in director["records"]:
        if record["clock_id"] != "ship":
            continue
        request, dispatch, later = record["request"], record["dispatch"], record["later_observation"]
        require(digest(request["observation"]) in observed, "visual_legacy_request_observation_binding")
        require(dispatch is not None and later is not None, "visual_legacy_later_observation_missing")
        require(digest(dispatch["observation"]) in observed and digest(later) in observed,
                "visual_legacy_execution_observation_binding")
        require(dispatch["time_s"] == dispatch["observation"]["time_s"]
                and request["observation"]["time_s"] <= dispatch["time_s"] < later["time_s"],
                "visual_legacy_observation_timeframe")
        if request["point"].startswith("deployment"):
            dispatched.append({"point": request["point"], "action": dispatch["action"],
                               "time_s": dispatch["time_s"]})
    require(digest(dispatched) == digest(executed), "visual_legacy_director_dispatch_binding")


def _validate_visual_handoff_prefix(m2):
    """Retain M2 handoff scope/state checks with arrived counters.

    Delayed separation confirmations are already bound to source/arrival
    receipts by ``validate_visual_origin``. The old immediate-counter interval
    condition is deliberately not applied to those delayed observations.
    """
    authority, profile, launch, handoff = (m2[key] for key in
        ("authority", "profile", "deployment_run", "handoff"))
    require(type(handoff) is dict and set(handoff) == {
        "schema", "inventory_basis", "development_only", "dispatch_authorized",
        "authority_sha256", "profile_sha256", "generation", "total_count", "released_count",
        "retained_count", "execution_state", "origin", "history", "release_observations", "handoff_sha256"}
        and handoff["schema"] == "missionos.deployment_return_handoff.v1"
        and handoff["inventory_basis"] == "synthetic_confirmed_separation_counter_v1"
        and handoff["development_only"] is True and handoff["dispatch_authorized"] is False,
        "visual_handoff_scope")
    require(handoff["handoff_sha256"] == digest({k: v for k, v in handoff.items() if k != "handoff_sha256"})
            and handoff["authority_sha256"] == digest(authority)
            and handoff["profile_sha256"] == digest(profile) == authority["profile_sha256"],
            "visual_handoff_identity")
    total, released, retained = (handoff[key] for key in ("total_count", "released_count", "retained_count"))
    require(all(type(value) is int for value in (total, released, retained, handoff["generation"],
                                                profile["payload"]["count"], launch["outcome"]["payload_released_count"]))
            and 1 <= total <= 26 and total == profile["payload"]["count"]
            and 0 <= released <= total and retained == total-released and handoff["generation"] >= 0
            and launch["outcome"]["payload_released_count"] == released,
            "visual_handoff_inventory_scalars")
    state = handoff["execution_state"]
    _state(state)
    require(digest(state) == digest(launch["final_state"])
            and launch["scenario"] == "launch"
            and launch["outcome"]["phase"] == launch["samples"][-1]["phase"] == "orbital_coast"
            and launch["outcome"]["orbit_gate_reached"] is True
            and launch["outcome"]["termination"] == "time_limit"
            and digest(state) == digest({key: launch["samples"][-1][key] for key in STATE}),
            "visual_handoff_state_or_phase")
    latest = handoff["history"][-1]
    require(latest["kind"] == "observation" and finite(latest["time_s"])
            and 0 <= state["time_s"]-latest["time_s"] <= 2.001
            and math.isclose(state["time_s"], authority["checkpoint_time_s"], rel_tol=0., abs_tol=1e-8)
            and digest(launch["outcome"]["duration_s"]) == digest(state["time_s"]),
            "visual_handoff_timeframe")
    _vehicle_inventory(profile, launch["final_vehicle"], retained)
    require(digest(handoff["origin"]) == digest(_observed_origin(state, retained)),
            "visual_return_observation_origin")
    events = [event for event in launch["events"] if event["event"] == "payload_released"]
    require(len(events) == len(handoff["release_observations"]) == len(launch["satellites"]) == released,
            "visual_handoff_release_count")
    require(all(finite(e["time_s"]) and 0 <= e["time_s"] <= state["time_s"] for e in events)
            and all(a["time_s"] < b["time_s"] for a, b in zip(events, events[1:])),
            "visual_handoff_release_chronology")
    require(all(digest(satellite["release_state"]["time_s"]) == digest(event["time_s"])
                for event, satellite in zip(events, launch["satellites"])),
            "visual_handoff_satellite_release_binding")
    require(not any(e["event"] in {"return_requested", "deorbit_ignition_command"} for e in launch["events"]),
            "visual_return_before_handoff")
    cutoffs = [event["time_s"] for event in launch["events"] if event["event"] == "orbit_cutoff_command"]
    require(len(cutoffs) == 1 and finite(cutoffs[0]) and 0 <= cutoffs[0] < state["time_s"],
            "visual_original_cutoff")
    scheduled = cutoffs[0] + profile["guidance"]["coast_before_return_s"]
    require(state["time_s"] < scheduled, "visual_handoff_after_original_return_schedule")
    return state, scheduled


def _validate_visual_handoff_scalars(study):
    """The return starts at the checked prefix with its original schedule."""
    m2 = study["m2"]
    state, scheduled = _validate_visual_handoff_prefix(m2)
    authority, launch, handoff, returning = (m2[key] for key in
        ("authority", "deployment_run", "handoff", "return_study"))
    require(digest(returning["deployment"]) == digest({"authority": authority, "handoff": handoff,
            "launch": launch, "visual_authority": study["authority"]})
            and digest(returning["launch"]) == digest(launch)
            and digest(returning["execution_origin"]) == digest(state), "visual_exact_state_return")
    require(digest(scheduled) == digest(returning["scheduled_s"]), "visual_original_return_schedule")
    require(not any(e["event"] in {"payload_released", "managed_mission_command", "visual_mission_command",
                    "visual_release_attempt_observed", "payload_release_attempt_acknowledged"}
                    for e in returning["execution"]["events"]), "visual_inventory_changed_after_handoff")


def _validate_pixel_provenance(study, expected_sources, frame=None):
    """Bind recorded pixels to the independently registered synthetic camera.

    These scalar registration values deliberately do not call the producer's
    camera factory, collector or renderer. A changed camera registration needs
    an explicit verifier/authority update, even when its record is rehashed.
    This checks source/configuration identity, not a re-rendered physical scene.
    """
    authority, camera_record = study["authority"], study["camera"]
    cfg = authority["visual"]
    expected_cameras = [
        {"camera_id": name, "width": 640, "height": 360, "horizontal_fov_deg": 75.,
         "near_m": .05, "far_m": 120., "eye_offset_m": [-2., side*4., 2.8],
         "look_offset_m": [3., 0., 0.], "up_body": [0., 0., 1.],
         "lens_covered": name == "deployment_port_primary" and authority["case"] == "primary_occluded"}
        for name, side in (("deployment_port_primary", -1), ("deployment_port_secondary", 1))
    ]
    expected_config = {"fps": cfg["camera_fps"], "latency_s": .5,
                       "history_s": cfg["ring_duration_s"], "max_age_s": cfg["frame_max_age_s"],
                       "ring_bytes": cfg["maximum_ring_bytes"], "store_bytes": cfg["maximum_store_bytes"],
                       "maximum_captures": 10000, "maximum_pinned_frames": 512}
    require(cfg["camera_ids"] == [c["camera_id"] for c in expected_cameras]
            and type(camera_record["cameras"]) is list
            and digest(camera_record["cameras"]) == digest(expected_cameras),
            "visual_registered_camera_configuration")
    require(type(camera_record["config"]) is dict
            and digest(camera_record["config"]) == digest(expected_config)
            and camera_record["renderer"] == "numpy_triangle_perspective_zbuffer_v1",
            "visual_registered_collection_configuration")
    renderer = expected_sources["src/runtime/starship_visual_observation.py"]
    require(type(renderer) is str and len(renderer) == 64
            and all(c in "0123456789abcdef" for c in renderer)
            and study["source_sha256"]["src/runtime/starship_visual_observation.py"] == renderer,
            "visual_frozen_renderer_source")
    if frame is not None:
        cameras = {c["camera_id"]: c for c in expected_cameras}
        require(frame["camera_id"] in cameras, "visual_frame_registered_camera")
        camera = cameras[frame["camera_id"]]
        require(frame["renderer_source_sha256"] == renderer
                and frame["capture_clock_id"] == "simulation"
                and frame["camera_config_sha256"] == digest(camera)
                and type(frame["width"]) is int and frame["width"] == camera["width"]
                and type(frame["height"]) is int and frame["height"] == camera["height"],
                "visual_frame_source_camera_clock_binding")


def verify_observation_evidence(authority, visual, launch, camera, expected_sources, http, ledger, artifact_dir):
    """Shared independent image/HTTP/ledger checks for visual and M3 records."""
    from .starship_visual_observation import FrameStore
    _visual_chain(authority, visual, launch)
    store = FrameStore(Path(artifact_dir) / "camera")
    expected_run_id = visual["run_id"]
    view = {"authority": authority, "camera": camera, "source_sha256": expected_sources}
    _validate_pixel_provenance(view, expected_sources)
    for request in visual["requests"]:
        for frame in request["manifest"]["frames"]:
            _validate_pixel_provenance(view, expected_sources, frame)
            require(sha256(store.read(frame)).hexdigest() == frame["image_sha256"], "visual_saved_pixels_hash")

    attempted = [((r["response"] or {}).get("provider_receipt") or {}) for r in visual["results"]
                 if ((r["response"] or {}).get("provider_receipt") or {}).get("local_http_attempted")]
    require(len(http) == len(attempted) <= 2, "visual_http_attempt_accounting")
    if attempted:
        require(ledger["run_id"] == expected_run_id and ledger["authority_sha256"] == digest(authority)
                and len(ledger["attempts"]) == len(attempted), "visual_durable_attempt_accounting")
        for receipt, observed, saved in zip(attempted, http, ledger["attempts"]):
            require(receipt["request_sha256"] == observed["request_sha256"] == saved["wire_sha256"],
                    "visual_exact_wire_binding")
            require(saved["attempt_id"] == receipt["attempt_id"] and saved["completed"] is True
                    and saved["status"] == receipt["status"] and saved["model_inference_invoked"] is False
                    and saved["semantic_request_sha256"] == receipt["semantic_request_sha256"],
                    "visual_ledger_completion_binding")
            request = next(r for r in visual["requests"] if r["request_id"] == saved["request_id"])
            require(saved["episode_id"] == request["episode_id"], "visual_ledger_episode_binding")
            from src.intelligence.starship_visual_agent import reconstruct_payload
            from src.intelligence.starship_visual_transport import canonical_bytes
            payload, binding = reconstruct_payload(request, store, receipt["asset_references"])
            wire = canonical_bytes(payload)
            require(sha256(wire).hexdigest() == receipt["request_sha256"]
                    and len(wire) == receipt["request_bytes"] == observed["request_bytes"]
                    and all(receipt[k] == v for k, v in binding.items()), "visual_reconstructed_wire_binding")
            require(sorted(observed["image_hashes"]) == sorted(f["image_sha256"] for f in request["manifest"]["frames"])
                    and observed["image_bytes"] == sum(f["image_bytes"] for f in request["manifest"]["frames"])
                    and observed["image_header"] is bool(request["manifest"]["frames"])
                    and observed["question_ids"] == sorted(payload["questions"]), "visual_http_image_binding")
            result_row = next(row for row in visual["results"] if row["request_id"] == request["request_id"])
            _validate_fixture_response(request, result_row, observed, payload)
    else:
        require(ledger is None or not ledger["attempts"], "visual_unrecorded_attempt")
    require(camera["capture_count"] > 0, "visual_normal_stream_not_captured")
    return True


def verify(study, *, expected_authority, expected_sources, expected_run_id, artifact_dir=None):
    result = {"schema": "missionos.starship_visual_verification.v1", "passed": False, "case_accepted": False,
              "issues": [], "case_issues": [], "mission_completed": False, "physical_execution": False,
              "native_model_efficacy_demonstrated": False, "independent_physics_validation": False}
    try:
        authority = validate_authority(expected_authority)
        require(artifact_dir is None or not (Path(artifact_dir) / "runtime-budget-failure.json").exists(),
                "visual_outer_runtime_budget_failed")
        require(study["schema"] == "missionos.starship_visual_study.v1" and study["authority"] == authority
                and study["source_sha256"] == expected_sources and study["run_id"] == expected_run_id,
                "visual_study_binding")
        plan = study["plan"]
        require(plan["plan_sha256"] == digest({k:v for k,v in plan.items() if k != "plan_sha256"})
                and plan["run_id"] == expected_run_id and plan["authority"] == authority
                and plan["source_sha256"] == expected_sources
                and plan["profile_sha256"] == digest(study["profile"]), "visual_frozen_plan_binding")
        require(study["runtime"]["sources_unchanged"] is True
                and study["runtime"]["hosted_model_calls"] == 0
                and study["runtime"]["automatic_retry"] is study["runtime"]["physical_execution"] is False,
                "visual_runtime_scope")
        require("runtime_failure" not in study, "visual_runtime_failure")
        m2, visual = study["m2"], study["visual"]
        launch, handoff, returning = m2["deployment_run"], m2["handoff"], m2["return_study"]
        require(visual == launch["visual_observation"] and visual["run_id"] == expected_run_id
                and m2["authority"] == authority["m2_authority"] and m2["source_sha256"] == expected_sources,
                "visual_m2_record_binding")
        expected_return_sources = _validate_m2_bindings(study, expected_sources, expected_run_id)
        receipts, attempts, releases = _visual_chain(authority, visual, launch)
        require(artifact_dir is not None and (Path(artifact_dir) / "camera").is_dir(),
                "visual_saved_pixels_required")
        http = study["http_fixture_requests"]
        ledger = study["attempt_ledger"]
        verify_observation_evidence(authority, visual, launch, study["camera"], expected_sources,
                                    http, ledger, artifact_dir)
        result.update(local_http_attempts=len(http), hosted_model_attempts=0,
                      visual_judgments=len(visual["results"]),
                      observation_requests=len(visual["observation_requests"]))
        if handoff is None:
            _closed_visual_episode(visual, launch["final_state"]["time_s"])
            _, _, latest, _ = _visual_trace(visual, m2["trace"], study["profile"]["payload"]["count"])
            _validate_visual_sequence_evidence(launch, m2["trace"], m2["authority"])
            require(authority["case"] == "unknown_inventory" and returning is None
                    and not m2["runtime"]["continuation_started"]
                    and m2.get("runtime_failure", {}).get("message") == "unresolved_inventory"
                    and visual["episode"] is not None
                    and visual["episode"]["closure_action"] == "stop_deployment"
                    and latest["time_s"] == launch["final_state"]["time_s"]
                    and latest["sequencer_state"] in {"skipped", "inhibited"}
                    and visual["observed_count"] != len(releases)
                    and not any(e["event"] in {"return_requested", "deorbit_ignition_command"} for e in launch["events"])
                    and not _fresh(receipts, attempts, launch["final_state"]["time_s"]),
                    "visual_unexpected_missing_handoff")
            _verify_launch_record(study["profile"], launch)
            result.update(passed=True, outcome="unresolved_inventory", retained_count=None,
                          case_issues=["inventory_unresolved_return_not_dispatched"])
            return result
        validate_visual_origin(authority, m2["authority"], handoff, launch, trace=m2["trace"])
        _validate_visual_sequence_evidence(launch, m2["trace"], m2["authority"])
        _validate_visual_handoff_scalars(study)
        require(handoff["handoff_sha256"] == digest({k:v for k,v in handoff.items() if k != "handoff_sha256"})
                and handoff["execution_state"] == launch["final_state"]
                and handoff["authority_sha256"] == digest(m2["authority"])
                and handoff["profile_sha256"] == digest(study["profile"]), "visual_handoff_identity")
        _state(handoff["execution_state"])
        _vehicle_inventory(study["profile"], launch["final_vehicle"], handoff["retained_count"])
        require(handoff["origin"] == _observed_origin(handoff["execution_state"], handoff["retained_count"]),
                "visual_return_observation_origin")
        require(returning["execution_origin"] == handoff["execution_state"] and returning["launch"] == launch
                and returning["deployment"] == {"authority": m2["authority"], "handoff": handoff,
                                                 "launch": launch, "visual_authority": authority},
                "visual_exact_state_return")
        from .starship_m2_decision_verifier import verify_decisions
        verify_decisions(m2)
        from .starship_replanning_verifier import verify as verify_return
        binding = {key: handoff[key] for key in ("handoff_sha256", "authority_sha256", "profile_sha256",
                                               "generation", "released_count", "retained_count")}
        from .starship_m2_verifier import _search_evidence
        _search_evidence(m2, binding)
        checked = verify_return(returning, expected_envelope=m2["authority"]["return_authority"],
            expected_case=m2["authority"]["return_case"], expected_sources=expected_return_sources,
            expected_run_id=expected_run_id+"-return", expected_deployment_binding=binding)
        result.update(return_verification=checked, outcome=checked["outcome"],
                      released_count=handoff["released_count"], retained_count=handoff["retained_count"])
        require(checked["passed"], "visual_return_record_verification")
        result["passed"] = True
        if authority["case"] == "normal":
            require(not visual["events"] and not http, "normal_visual_model_call")
        elif authority["case"] in {"delayed_confirmation", "primary_occluded"}:
            require(len(http) == 2 and visual["episode"]["closure_action"] == "continue", "visual_resume_case_incomplete")
        else:
            require(visual["episode"]["closure_action"] == "stop_deployment", "visual_stop_case_incomplete")
        result.update(_visual_case_acceptance(authority["case"], handoff, returning, checked))
    except (KeyError, ValueError, TypeError, IndexError, OSError, OverflowError) as error:
        result["passed"] = result["case_accepted"] = False
        result["issues"].append(str(error) if isinstance(error, ValueError) else "malformed_visual_record:"+type(error).__name__)
    return result

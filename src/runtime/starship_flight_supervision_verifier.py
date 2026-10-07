"""Independent checks of saved in-flight deployment-supervision evidence.

No simulator, provider, controller, or socket is imported here. These checks
establish record consistency and a subsequent observed sequencer effect. The
caller binds the approved plan and process; this module does not authenticate
a human, certify flight dynamics, or demonstrate any model's added value.
"""
from __future__ import annotations

import math

_MU = 3.986004418e14
_EARTH_A = 6_378_137.0
_ACTIONS = ["hold", "skip_remaining_deployment"]
_ROUTES = {"bounded", "need_observation", "human_review", "deep_reasoning"}
_OBS_FIELDS = {"observation_id", "sequence", "time_s", "phase", "payload_released_count",
               "release_attempt_count", "release_acknowledged", "sequencer_state",
               "perigee_altitude_m", "dynamic_pressure_pa", "body_rate_rad_s",
               "propellant_kg", "return_deadline_s"}
_STATE_FIELDS = {"observation_id", "sequence", "release_attempt_count", "release_acknowledged",
                 "sequencer_state", "payload_released_count"}


class _Invalid(Exception):
    def __init__(self, code, detail):
        self.issue = {"code": code, "detail": detail}


def _require(condition, code, detail):
    if not condition:
        raise _Invalid(code, detail)


def _number(value):
    return type(value) in (int, float) and abs(value) < 1e100 and math.isfinite(value)


def _near(first, second, tolerance=1e-6):
    return _number(first) and _number(second) and abs(first-second) <= tolerance


def _vector(value):
    return type(value) is list and len(value) == 3 and all(_number(x) and abs(x) < 1e15 for x in value)


def _norm(value):
    return math.sqrt(sum(x*x for x in value))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _orbit(sample):
    r, v = sample["r_eci_m"], sample["v_eci_mps"]
    radius = _norm(r)
    _require(radius > 1., "observation_state", "Position is degenerate")
    h = _cross(r, v)
    eccentricity = _norm([a/_MU-b/radius for a, b in zip(_cross(v, h), r)])
    energy = sum(x*x for x in v)/2-_MU/radius
    return energy < 0 and eccentricity < 1, sum(x*x for x in h)/(_MU*(1+eccentricity))-_EARTH_A


def _json(value):
    stack, active, count = [(value, 0, False)], set(), 0
    while stack:
        row, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(row))
            continue
        count += 1
        _require(count <= 100_000 and depth <= 32, "input_limit", "Supervision record exceeds traversal limits")
        if row is None or type(row) is bool:
            continue
        if type(row) in (int, float):
            _require(_number(row), "invalid_number", "Expected bounded finite number")
        elif type(row) is str:
            _require(len(row) <= 16_384, "input_limit", "String exceeds record limit")
        elif type(row) in (dict, list):
            _require(id(row) not in active and len(row) <= 10_000, "invalid_json", "Cyclic or oversized record")
            active.add(id(row))
            stack.append((row, depth, True))
            if type(row) is dict:
                _require(all(type(key) is str and len(key) <= 256 for key in row), "invalid_json", "Invalid object key")
                stack.extend((child, depth+1, False) for child in row.values())
            else:
                stack.extend((child, depth+1, False) for child in row)
        else:
            raise _Invalid("invalid_json", "Expected JSON-compatible record")


def _contract(contract):
    if contract is None:
        return
    _require(type(contract) is dict, "approval_scope", "Expected approved supervision scope")
    _require(contract.get("procedure_id") in ("local_deployment_missing_effect_v1", "local_deployment_status_collection_v1")
             and contract.get("allowed_actions") == _ACTIONS and type(contract.get("maximum_commands")) is int
             and contract["maximum_commands"] == 1 and _near(contract.get("decision_expiry_s"), 75.)
             and contract.get("physical_execution_authorized") is False,
             "approval_scope", "Approved scope differs from the fixed one-command simulation procedure")
    if contract["procedure_id"] == "local_deployment_status_collection_v1":
        _require(type(contract.get("maximum_observation_requests")) is int and contract["maximum_observation_requests"] == 1
                 and contract.get("observation_kind") == "deployment_status"
                 and contract.get("collection_does_not_extend_deadline") is True,
                 "approval_scope", "Collection must be one bounded report without a new deadline")
        mode = contract.get("mode")
        _require(mode in ("fixture", "live") and type(contract.get("maximum_jev_calls")) is int
                 and contract["maximum_jev_calls"] == (2 if mode == "live" else 0)
                 and type(contract.get("maximum_deepseek_calls")) is int
                 and contract["maximum_deepseek_calls"] == (1 if mode == "live" else 0),
                 "approval_scope", "Collection provider budgets must match the approved mode")


def _provider_budget(record, contract):
    """Count declared attempts, not independently authenticate network calls."""
    responses = [record[key] for key in ("response", "followup_response") if record.get(key) is not None]
    modes, counts = set(), {"jev": 0, "llm": 0}
    flags = ("call_attempted", "call_succeeded", "response_received", "complete_response_observed", "model_inference_invoked")
    for response in responses:
        _require(type(response) is dict and set(response) == {"request_id", "observation_id", "action", "route", "jev_invocation", "llm_invocation"},
                 "provider_budget", "Each assessment needs exactly its two provider receipts")
        for role in counts:
            item = response[role+"_invocation"]
            _require(type(item) is dict and item.get("schema_version") == "runtime_invocation_evidence.v1"
                     and all(type(item.get(flag)) is bool for flag in flags)
                     and item.get("mode") in ("fixture", "live") and type(item.get("maximum_instance_calls")) is int
                     and item["maximum_instance_calls"] == 1 and type(item.get("retries")) is int
                     and item["retries"] == 0 and item.get("call_succeeded") is item.get("model_inference_invoked"),
                     "provider_budget", "Missing or inconsistent one-attempt invocation evidence")
            modes.add(item["mode"])
            attempted = item["call_attempted"]
            _require((type(item.get("reserved_call_slot")) is int and item["reserved_call_slot"] == 1 if attempted
                      else item.get("reserved_call_slot") is None)
                     and (attempted or not any(item[flag] for flag in flags[1:]))
                     and (not item["model_inference_invoked"] or item["response_received"] and item["complete_response_observed"])
                     and (item["mode"] == "live" or not any(item[flag] for flag in flags)),
                     "provider_budget", "Fixture/receipt state cannot claim a provider attempt or inference")
            if role == "llm" and response["route"] != "deep_reasoning":
                _require(not attempted and item.get("status") == "not_routed", "provider_budget", "Reasoning was not routed")
            counts[role] += int(attempted)
    expected_mode = contract.get("mode") if contract is not None else next(iter(modes), "fixture")
    _require(len(modes) <= 1 and (not modes or modes == {expected_mode})
             and counts["jev"] <= (2 if expected_mode == "live" else 0)
             and counts["llm"] <= (1 if expected_mode == "live" else 0),
             "provider_budget", "Recorded calls exceed the bound assessment budgets")
    return {"recorded_jev_attempt_count": counts["jev"], "recorded_deepseek_attempt_count": counts["llm"]}


def _collection(record, request, response, linked, profile, contract):
    """Reconstruct the read request and later report from linked saved states."""
    allowed = record.get("observation_collection_allowed", False)
    _require(type(allowed) is bool and (contract is None or allowed is
             (contract.get("procedure_id") == "local_deployment_status_collection_v1")),
             "collection_scope", "Additional observation was not in the approved scope")
    operation = record.get("collection")
    following, reply = record.get("followup_request"), record.get("followup_response")
    if operation is None:
        _require(following is None and reply is None, "collection_binding", "Reassessment lacks a report request")
        return request, response, False
    _require(allowed and type(operation) is dict and type(response) is dict
             and set(response) == {"request_id", "observation_id", "action", "route", "jev_invocation", "llm_invocation"}
             and response.get("route") == "need_observation" and response.get("action") == "hold"
             and response.get("request_id") == request["request_id"]
             and response.get("observation_id") == request["observations"][-1]["observation_id"]
             and type(response.get("jev_invocation")) is dict and type(response.get("llm_invocation")) is dict,
             "collection_binding", "Report collection must follow a bound, non-commanding observation route")
    _require(operation.get("collection_id") == request["request_id"]+"-status-1"
             and operation.get("kind") == "deployment_status" and operation.get("accepted") is True
             and operation.get("actuator_operation") is False and operation.get("physical_execution") is False
             and operation.get("request_observation_id") in linked,
             "collection_binding", "Unknown observation operation or missing request observation")
    issued, _, bound = linked[operation["request_observation_id"]]
    origin = request["observations"][-1]
    _require(_near(operation.get("issued_time_s"), issued["time_s"]) and issued["time_s"] > origin["time_s"]
             and issued["time_s"]-origin["time_s"] <= 75.+1e-6 and issued["time_s"] < issued["return_deadline_s"]
             and issued["sequencer_state"] == "inhibited" and issued["payload_released_count"] == 0
             and issued["release_acknowledged"] is True and issued["release_attempt_count"] == 1
             and issued["propellant_kg"] >= profile["ship"]["return_reserve_kg"] and bound,
             "collection_rules", "Report request escaped its fresh-state or original deadline constraints")
    if operation.get("status") == "requested":
        _require(operation.get("completed_time_s") is None and operation.get("received_observation_id") is None
                 and following is None and reply is None,
                 "collection_binding", "Accepted request cannot imply a received report")
        return request, None, False
    _require(operation.get("status") == "report_received" and operation.get("received_observation_id") in linked,
             "collection_binding", "Report completion requires a saved, measured observation")
    received, _, _ = linked[operation["received_observation_id"]]
    _require(_near(operation.get("completed_time_s"), received["time_s"])
             and received["time_s"] >= issued["time_s"]+2.-1e-6
             and received["sequence"] == issued["sequence"]+1
             and received["time_s"]-origin["time_s"] <= 75.+1e-6
             and received["time_s"] < received["return_deadline_s"]
             and type(following) is dict and set(following) == {"schema", "request_id", "observations", "allowed_actions"}
             and following.get("schema") == request["schema"] and following.get("request_id") == request["request_id"]
             and following.get("allowed_actions") == _ACTIONS and following.get("observations") == [issued, received],
             "collection_freshness", "Reassessment must use the newly collected report without resetting the deadline")
    return following, reply, True


def _observations(record, samples, profile):
    observations = record.get("observations")
    _require(type(observations) is list and len(observations) <= 1000, "observations", "Expected bounded observations")
    saved = {}
    previous_time = -1.
    for sample in samples:
        _require(type(sample) is dict and _number(sample.get("time_s")) and sample["time_s"] >= previous_time,
                 "samples", "Saved sample time must be ordered")
        previous_time = sample["time_s"]
        telemetry = sample.get("flight_supervision")
        if telemetry is not None:
            _require(type(telemetry) is dict, "sample_telemetry", "Sequencer telemetry must be an object")
            observation_id = telemetry.get("observation_id")
            if observation_id is not None:
                _require(type(observation_id) is str, "sample_telemetry", "Invalid observation identifier")
                saved.setdefault(observation_id, []).append(sample)
    linked, prior = {}, None
    for observation in observations:
        _require(type(observation) is dict and set(observation) == _OBS_FIELDS, "observation_schema",
                 "Only measured, allowlisted observation fields are admitted")
        sequence = observation["sequence"]
        _require(type(sequence) is int and sequence >= 1 and type(observation["observation_id"]) is str,
                 "observation_identity", "Invalid observation sequence or identity")
        _require(observation["observation_id"] not in linked, "observation_identity", "Observation identity was repeated")
        _require(_number(observation["time_s"]) and observation["time_s"] >= 0,
                 "observation_time", "Invalid simulation time")
        if prior:
            _require(sequence > prior["sequence"] and observation["time_s"] > prior["time_s"],
                     "observation_freshness", "Observations require distinct advancing time and sequence")
        prior = observation
        candidates = [sample for sample in saved.get(observation["observation_id"], [])
                      if _near(sample["time_s"], observation["time_s"])]
        _require(bool(candidates), "observation_sample_binding", "Observation lacks a matching saved physical state")
        sample = candidates[0]
        for candidate in candidates:
            _require(all(candidate.get(field) == sample.get(field) for field in
                         ("r_eci_m", "v_eci_mps", "omega_body_rad_s", "propellant_kg", "flight_supervision")),
                     "observation_sample_binding", "One observation identifies conflicting saved states")
        _require(all(sample["flight_supervision"].get(field) == observation[field] for field in _STATE_FIELDS),
                 "observation_sample_binding", "Observation differs from saved sequencer telemetry")
        _require(observation["phase"] == sample.get("phase") == "orbital_coast",
                 "observation_phase", "Decision observations must be from orbital coast")
        for field in ("r_eci_m", "v_eci_mps", "omega_body_rad_s"):
            _require(_vector(sample.get(field)), "observation_state", "Invalid saved physical state vector")
        bound, perigee = _orbit(sample)
        _require(_near(observation["perigee_altitude_m"], perigee, 1e-4)
                 and _near(observation["body_rate_rad_s"], _norm(sample["omega_body_rad_s"]))
                 and _near(observation["propellant_kg"], sample.get("propellant_kg"))
                 and _near(observation["dynamic_pressure_pa"], sample.get("dynamic_pressure_pa")),
                 "observation_state_binding", "Measured fields differ from saved position, velocity, rate, fuel or pressure")
        _require(observation["propellant_kg"] >= 0 and observation["dynamic_pressure_pa"] >= 0
                 and type(observation["release_attempt_count"]) is int and observation["release_attempt_count"] == 1
                 and observation["release_acknowledged"] is True
                 and type(observation["payload_released_count"]) is int and observation["payload_released_count"] == 0
                 and observation["sequencer_state"] in ("inhibited", "skipped")
                 and _number(observation["return_deadline_s"]),
                 "observation_range", "Invalid operational observation")
        linked[observation["observation_id"]] = (observation, sample, bound)
    return observations, linked


def verify_supervision(run, profile, *, expected_request_id=None, expected_contract=None):
    """Return bounded findings; ``passed`` and ``observed_effect`` are separate.

    A recorded hold, provider timeout, or untriggered anomaly may pass integrity
    with no observed command effect. Approval authentication remains the caller's
    responsibility; an ACK alone never establishes the sequencer state change.
    """
    result = {"schema": "missionos.starship_flight_supervision_verification.v1", "passed": False,
              "issues": [], "observed_effect": False, "command_count": 0, "route": None,
              "scope": "saved observation, fixed command scope, Rules and subsequent sequencer state consistency; not approval authentication or physical execution",
              "mission_completed": False, "physical_execution": False, "model_value_demonstrated": False}
    try:
        _contract(expected_contract)
        _require(expected_request_id is None or type(expected_request_id) is str and bool(expected_request_id),
                 "expected_request_id", "Expected request identity must be a nonempty string")
        _require(type(run) is dict and run.get("scenario") == "deployment_no_effect", "scenario", "Unsupported supervised scenario")
        _require(type(profile) is dict and all(type(profile.get(key)) is dict for key in ("payload", "guidance", "ship")),
                 "profile", "Missing procedure limits")
        interval = profile["payload"].get("interval_s")
        reserve = profile["ship"].get("return_reserve_kg")
        _require(_number(interval) and interval > 0 and _number(reserve) and reserve >= 0,
                 "profile", "Invalid release interval or propellant reserve")
        record = run.get("supervision")
        _json(record)
        _require(type(record) is dict and record.get("schema") == "missionos.starship_flight_supervision.v1"
                 and record.get("physical_execution") is False, "supervision_schema", "Missing bounded simulation supervision record")
        if record.get("observation_collection_allowed") is True:
            result.update(_provider_budget(record, expected_contract))
        request_id = record.get("request_id")
        _require(type(request_id) is str and 1 <= len(request_id) <= 256
                 and (expected_request_id is None or request_id == expected_request_id),
                 "request_binding", "Supervision request differs from the approved execution")
        _require(_near(record.get("max_decision_age_s"), 75.)
                 and _near(record.get("observation_interval_s"), 2.)
                 and (record.get("effect_observation_interval_s") is None or _near(record["effect_observation_interval_s"], interval)),
                 "procedure_limits", "Recorded procedure limits differ from fixed limits")
        samples = run.get("samples")
        _require(type(samples) is list and 1 <= len(samples) <= 250_000, "samples", "Missing bounded physical trajectory")
        observations, linked = _observations(record, samples, profile)
        _require(not observations or _near(record.get("effect_observation_interval_s"), interval),
                 "procedure_limits", "Triggered supervision requires its effect observation interval")
        commands, rules = record.get("commands"), record.get("rules")
        _require(type(commands) is list and len(commands) <= 1 and type(rules) is list and len(rules) <= 2,
                 "command_budget", "Only one bounded dispatch is authorized")
        result["command_count"] = len(commands)
        status = record.get("status")
        _require(status in ("not_triggered", "no_broker_hold", "held", "rejected", "expired",
                            "effect_observations_recorded", "mission_ended_before_evidence"),
                 "terminal_status", "Expected a terminal supervision record")
        final = samples[-1].get("flight_supervision")
        permitted_final_states = ("inhibited", "skipped") if commands else ("inhibited",) if observations else (None,)
        _require(type(final) is dict and final.get("sequencer_state") in permitted_final_states
                 and final.get("payload_released_count") == 0
                 and record.get("final_sequencer_state") == final.get("sequencer_state")
                 and _near(record.get("final_time_s"), samples[-1]["time_s"]),
                 "final_telemetry", "Terminal record must agree with saved final sequencer telemetry and simulation time")
        request, response = record.get("request"), record.get("response")
        events = run.get("events")
        _require(type(events) is list and len(events) <= 20_000 and all(type(event) is dict for event in events),
                 "events", "Expected bounded operation events")
        attempts = [event for event in events if event.get("event") == "payload_release_attempt_acknowledged"]
        skips = [event for event in events if event.get("event") == "deployment_skip_command"]
        releases = [event for event in events if event.get("event") == "payload_released"]
        _require(len(attempts) <= 1 and len(skips) == len(commands) and not releases and run.get("satellites") == [],
                 "event_binding", "Supervised missing-effect case must retain an unreleased payload")
        outcome = run.get("outcome")
        _require(type(outcome) is dict and type(outcome.get("payload_released_count")) is int
                 and outcome["payload_released_count"] == 0,
                 "payload_outcome", "Final outcome must agree with the absence of finite payload separation")
        if request is None:
            _require(response is None and not commands and not rules and len(observations) < 2,
                     "request_binding", "Decision evidence exists without the two-observation request")
            _require(status in ("not_triggered", "mission_ended_before_evidence"),
                     "terminal_status", "Unrequested operation cannot claim a completed decision")
            result["passed"] = True
            return result
        _require(type(request) is dict and set(request) == {"schema", "request_id", "observations", "allowed_actions"}
                 and request.get("schema") == "missionos.starship_flight_supervision_request.v1"
                 and request.get("request_id") == request_id and request.get("allowed_actions") == _ACTIONS
                 and len(observations) >= 2 and request.get("observations") == observations[:2],
                 "request_binding", "Request must contain exactly the first two measured observations and fixed actions")
        first, last = observations[:2]
        _require(last["time_s"]-first["time_s"] >= 2.-1e-6
                 and first["sequencer_state"] == last["sequencer_state"] == "inhibited"
                 and len(attempts) == 1 and _number(attempts[0].get("time_s"))
                 and attempts[0]["time_s"] <= first["time_s"],
                 "missing_effect_evidence", "Decision requires two subsequent inhibited/no-separation observations after the attempt")
        cutoffs = [event for event in events if event.get("event") == "orbit_cutoff_command"]
        coast = profile["guidance"].get("coast_before_return_s")
        _require(len(cutoffs) == 1 and _number(cutoffs[0].get("time_s")) and _number(coast)
                 and all(_near(observation["return_deadline_s"], cutoffs[0]["time_s"]+coast) for observation in observations),
                 "deadline_binding", "Return deadline must derive from recorded orbit cutoff and configured coast interval")
        original_last = last
        request, response, report_received = _collection(record, request, response, linked, profile, expected_contract)
        result.update(observation_collection_requested=record.get("collection") is not None,
                      observation_collection_completed=report_received,
                      initial_route=(record.get("response") or {}).get("route"))
        last = request["observations"][-1]
        if response is None:
            _require(not commands and status in ("no_broker_hold", "rejected", "expired", "mission_ended_before_evidence"),
                     "response_binding", "No command or completed decision may be inferred from a missing response")
            for rule in rules:
                _require(type(rule) is dict and rule.get("action") == "hold" and rule.get("allowed") is False
                         and type(rule.get("reasons")) is list and len(rule["reasons"]) == 1
                         and rule["reasons"][0] in ("no_broker", "decision_expired", "return_deadline", "phase_ended", "malformed_response"),
                         "rules_record", "Missing response must retain a non-commanding expiry, rejection or hold")
                if rule["reasons"] != ["phase_ended"]:
                    _require(type(rule.get("observation_id")) is str and rule["observation_id"] in linked
                             and _near(rule.get("time_s"), linked[rule["observation_id"]][0]["time_s"]),
                             "rules_freshness", "Hold decision must refer to a real observation")
                else:
                    _require(_number(rule.get("time_s")) and last["time_s"] <= rule["time_s"] <= samples[-1]["time_s"],
                             "rules_freshness", "Phase expiry must occur within the subsequent trajectory")
            result["passed"] = True
            return result
        _require(type(response) is dict and response.get("request_id") == request_id
                 and response.get("observation_id") == last["observation_id"]
                 and response.get("action") in _ACTIONS and type(response.get("route")) is str and response["route"] in _ROUTES
                 and type(response.get("jev_invocation")) is dict and type(response.get("llm_invocation")) is dict,
                 "response_binding", "Proposal must refer to the actual request and its latest observation")
        result["route"] = response["route"]
        _require(len(rules) == 1, "rules_record", "A consumed proposal requires one Rules decision")
        rule = rules[0]
        _require(type(rule) is dict and type(rule.get("observation_id")) is str and rule["observation_id"] in linked and rule.get("action") == response["action"]
                 and type(rule.get("allowed")) is bool and type(rule.get("reasons")) is list,
                 "rules_record", "Missing fresh observation or action binding in Rules")
        observed, _, bound = linked[rule["observation_id"]]
        _require(_near(rule.get("time_s"), observed["time_s"]) and observed["time_s"] > last["time_s"],
                 "rules_freshness", "Rules must evaluate the current distinct observation")
        eligible = (response["action"] == "skip_remaining_deployment" and response["route"] in {"bounded", "deep_reasoning"}
                    and observed["sequencer_state"] == "inhibited" and bound
                    and observed["time_s"]-original_last["time_s"] <= 75.+1e-6
                    and observed["time_s"] < observed["return_deadline_s"]
                    and observed["propellant_kg"] >= reserve)
        _require(not rule["allowed"] or eligible, "rules_constraint", "Rules allowed an operation outside the bounded fresh-state constraints")
        _require(bool(commands) == (rule["allowed"] and response["action"] == "skip_remaining_deployment"),
                 "dispatch_binding", "Dispatch differs from the actual Rules decision")
        if not commands:
            _require(all(item["sequencer_state"] == "inhibited" for item in observations),
                     "uncommanded_effect", "Sequencer skipped without an admitted command")
            _require(status in ("held", "rejected"), "terminal_status", "Uncommanded proposal must remain held or rejected")
            result["passed"] = True
            return result
        command = commands[0]
        _require(type(command) is dict and command.get("command_id") == request_id+"-skip-1"
                 and command.get("request_id") == request_id and command.get("action") == "skip_remaining_deployment"
                 and command.get("observation_id") == observed["observation_id"]
                 and _near(command.get("time_s"), observed["time_s"])
                 and command.get("accepted") is True and command.get("sequencer_state_before") == "inhibited"
                 and command.get("sequencer_state_after") == "skipped" and command.get("payload_released_count") == 0,
                 "command_binding", "Command must implement the single admitted, request-bound operation")
        _require(skips[0].get("command_id") == command["command_id"] and _near(skips[0].get("time_s"), command["time_s"]),
                 "command_event_binding", "Command lacks matching simulator operation event")
        later = [item for item in observations if item["time_s"] > command["time_s"]]
        _require(all(item["sequencer_state"] == "inhibited" for item in observations if item["time_s"] <= command["time_s"]),
                 "sequencer_transition", "Sequencer cannot skip before the admitted operation")
        complete = (len(later) >= 2 and later[0]["time_s"] >= command["time_s"]+interval-1e-6
                    and later[-1]["time_s"]-later[0]["time_s"] >= interval-1e-6
                    and later[-1]["time_s"] >= command["time_s"]+2*interval-1e-6)
        # Complete observations may truthfully show an ACK without an effect.
        # The recorded status names evidence collection, not recovery success.
        effect = (complete and all(item["sequencer_state"] == "skipped" for item in later)
                  and final["sequencer_state"] == "skipped")
        _require(samples[-1]["time_s"] >= command["time_s"], "final_telemetry", "Final state precedes the command")
        _require(status == ("effect_observations_recorded" if complete else "mission_ended_before_evidence"),
                 "effect_status", "Recorded effect status requires two subsequent, properly spaced observations")
        result["observed_effect"] = effect
        result["passed"] = True
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    return result

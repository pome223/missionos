"""Independent typed tower-supervision checks, without control or inference.

A signed resolution is transaction integrity, not independent human identity or
ledger completeness. A caller's application report becomes an observed mode
change only when separate saved controller observations bind before and after.
Neither a mode flag nor a verifier pass proves diversion, capture or improvement.
"""

from __future__ import annotations

from hashlib import sha256
import hmac
import json
import math
import re

from .starship_operator_resolution import verify_operator_resolution

_SCOPE = "preapproved_local_simulation_capture_or_divert_v1"
_ACTIONS = ["continue_capture", "divert"]
_ROUTES = ["bounded", "need_observation", "human_review", "deep_reasoning"]
_CONTEXT = {"session_id", "plan_id", "plan_sha256", "run_id", "request_id"}
_SCOPE_FIELDS = {
    "schema",
    "scope",
    "context",
    "approval_record_sha256",
    "source_sha256",
    "issued_simulation_time_s",
    "issued_wall_time_s",
    "original_simulation_deadline_s",
    "original_wall_deadline_s",
    "maximum_observation_age_s",
    "observation_collection_allowed",
    "human_resolution_allowed",
}
_OBSERVATION_FIELDS = {
    "schema",
    "context",
    "observation_id",
    "state",
    "state_sha256",
    "evidence_sha256",
    "simulation_time_s",
    "wall_time_s",
    "tower_ready",
    "return_mode",
}
_STATE_FIELDS = {
    "time_s",
    "r_eci_m",
    "v_eci_mps",
    "q_body_to_eci",
    "omega_body_rad_s",
    "propellant_kg",
    "engine_states",
    "flap_angles_rad",
}
_REQUEST_FIELDS = {
    "schema",
    "context",
    "source_sha256",
    "approval_record_sha256",
    "round",
    "observation",
    "issued_simulation_time_s",
    "issued_wall_time_s",
    "original_simulation_deadline_s",
    "original_wall_deadline_s",
    "maximum_observation_age_s",
    "allowed_routes",
    "allowed_actions",
    "numeric_flight_authority",
    "sha256",
}
_PROPOSAL_FIELDS = {
    "schema",
    "request_sha256",
    "context_sha256",
    "source_sha256",
    "observed_evidence_sha256",
    "route",
    "proposed_action",
    "classifier_evidence",
    "approval_granted",
    "executor_command_issued",
    "numeric_flight_authority",
}
_TICK_FIELDS = {
    "observation_evidence_sha256",
    "simulation_time_s",
    "wall_time_s",
    "run_active",
    "source_sha256",
    "plan_sha256",
    "routing_proposal_sha256",
    "human_decision_sha256",
}
_LOG_FIELDS = {
    "schema",
    "approved_scope",
    "observations",
    "routing_requests",
    "routing_proposals",
    "tick_inputs",
    "observation_collection",
    "human_resolution",
    "rules",
    "accepted_decision",
    "directive",
    "application_report",
    "terminal_outcome",
    "status",
    "approval_independently_verified",
    "physical_effect_verified",
    "mission_completed",
    "model_value_established",
    "physical_execution",
    "ditch_trajectory_implemented",
    "llm_calls",
}


class _Invalid(ValueError):
    def __init__(self, code, detail):
        self.issue = {"code": code, "detail": detail}


def _require(condition, code, detail):
    if not condition:
        raise _Invalid(code, detail)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _clock(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _json(value):
    stack, active, count = [(value, 0, False)], set(), 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        count += 1
        _require(
            count <= 2_000_000 and depth <= 48,
            "input_limit",
            "Tower evidence exceeds its finite traversal budget",
        )
        if item is None or type(item) is bool:
            continue
        if type(item) in (int, float):
            _require(
                math.isfinite(item) and abs(item) <= 1e12,
                "number",
                "Nonfinite or oversized tower evidence",
            )
        elif type(item) is str:
            _require(len(item) <= 16384, "input_limit", "Oversized tower evidence string")
        elif type(item) in (dict, list):
            _require(
                id(item) not in active and len(item) <= 10000,
                "input_limit",
                "Cyclic or oversized tower evidence",
            )
            active.add(id(item))
            stack.append((item, depth, True))
            if type(item) is dict:
                _require(
                    all(type(k) is str and len(k) <= 256 for k in item),
                    "input_limit",
                    "Invalid evidence key",
                )
                stack.extend((x, depth + 1, False) for x in item.values())
            else:
                stack.extend((x, depth + 1, False) for x in item)
        else:
            raise _Invalid("json", "Expected plain saved JSON evidence")


def _context(value):
    _require(
        type(value) is dict
        and set(value) == _CONTEXT
        and _hash(value["plan_sha256"])
        and all(
            type(value[k]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value[k])
            for k in ("session_id", "plan_id", "run_id", "request_id")
        ),
        "context",
        "Invalid closed tower context",
    )


def _state(value):
    _require(
        type(value) is dict and set(value) == _STATE_FIELDS and _clock(value["time_s"]),
        "state",
        "Invalid full observed state",
    )
    for field, length in (
        ("r_eci_m", 3),
        ("v_eci_mps", 3),
        ("q_body_to_eci", 4),
        ("omega_body_rad_s", 3),
    ):
        vector = value[field]
        _require(
            type(vector) is list
            and len(vector) == length
            and all(
                type(x) in (int, float) and math.isfinite(x) and abs(x) <= 1e12 for x in vector
            ),
            "state",
            "Invalid observed state vector",
        )
    _require(
        abs(sum(x * x for x in value["q_body_to_eci"]) - 1.0) <= 1e-8
        and _clock(value["propellant_kg"])
        and type(value["engine_states"]) is list
        and 1 <= len(value["engine_states"]) <= 128
        and type(value["flap_angles_rad"]) is list
        and len(value["flap_angles_rad"]) <= 32
        and all(type(x) in (int, float) and math.isfinite(x) for x in value["flap_angles_rad"]),
        "state",
        "Invalid observed actuators or mass",
    )
    for engine in value["engine_states"]:
        _require(
            type(engine) is dict
            and set(engine) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(engine["available"]) is bool
            and type(engine["throttle"]) in (int, float)
            and 0 <= engine["throttle"] <= 1
            and all(
                type(engine[k]) in (int, float) and math.isfinite(engine[k])
                for k in ("gimbal_x_rad", "gimbal_y_rad")
            ),
            "state",
            "Invalid finite observed engine state",
        )


def _observation(value, scope):
    _require(
        type(value) is dict
        and set(value) == _OBSERVATION_FIELDS
        and value["schema"] == "missionos.starship_tower_observation.v1",
        "observation",
        "Invalid closed tower observation",
    )
    _context(value["context"])
    _state(value["state"])
    _require(
        value["context"] == scope["context"]
        and type(value["observation_id"]) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["observation_id"])
        and value["state_sha256"] == _digest(value["state"])
        and value["evidence_sha256"]
        == _digest({k: v for k, v in value.items() if k != "evidence_sha256"})
        and _clock(value["simulation_time_s"])
        and value["simulation_time_s"] == value["state"]["time_s"]
        and _clock(value["wall_time_s"])
        and (value["tower_ready"] is None or type(value["tower_ready"]) is bool)
        and value["return_mode"] in ("capture", "divert", "undecided"),
        "observation",
        "Observation context, state, clock or evidence hash differs",
    )


def _fresh(obs, tick, scope):
    age = scope["maximum_observation_age_s"]
    return (
        0 <= tick["simulation_time_s"] - obs["simulation_time_s"] <= age
        and 0 <= tick["wall_time_s"] - obs["wall_time_s"] <= age
    )


def _expired(tick, scope):
    return (
        tick["simulation_time_s"] >= scope["original_simulation_deadline_s"]
        or tick["wall_time_s"] >= scope["original_wall_deadline_s"]
    )


def _ledger_obs(obs):
    return {
        key: obs[key]
        for key in ("evidence_sha256", "simulation_time_s", "wall_time_s", "tower_ready")
    }


def _pending_hmac(request, scope, observations, key):
    fields = {
        "schema",
        "scope",
        "context",
        "observed_evidence_sha256",
        "initial_observation",
        "allowed_actions",
        "issued_simulation_time_s",
        "issued_wall_time_s",
        "original_simulation_deadline_s",
        "original_wall_deadline_s",
        "maximum_observation_age_s",
        "simulation_only",
        "numeric_flight_authority",
        "limits_waivable",
        "sha256",
        "signature",
    }
    _require(
        type(key) is bytes and 32 <= len(key) <= 128,
        "human_signature",
        "Signed human records require their verification key",
    )
    _require(
        type(request) is dict
        and set(request) == fields
        and request["schema"] == "missionos.starship_operator_resolution_request.v1"
        and request["scope"] == _SCOPE
        and request["context"] == scope["context"]
        and request["allowed_actions"] == _ACTIONS
        and request["simulation_only"] is True
        and request["numeric_flight_authority"] is False
        and request["limits_waivable"] is False,
        "human_scope",
        "Signed resolution cannot create wider authority",
    )
    obs = observations.get(request["observed_evidence_sha256"])
    _require(
        obs is not None
        and request["initial_observation"] == _ledger_obs(obs)
        and request["sha256"]
        == _digest({k: v for k, v in request.items() if k not in ("sha256", "signature")})
        and _hash(request["signature"])
        and hmac.compare_digest(
            request["signature"],
            hmac.new(
                key, _canonical({k: v for k, v in request.items() if k != "signature"}), "sha256"
            ).hexdigest(),
        ),
        "human_signature",
        "Pending human request is not the bound signed transaction",
    )
    for field in (
        "original_simulation_deadline_s",
        "original_wall_deadline_s",
        "maximum_observation_age_s",
    ):
        _require(
            request[field] == scope[field],
            "human_deadline",
            "Human handover extended the approved original deadline",
        )
    issued = {
        "simulation_time_s": request["issued_simulation_time_s"],
        "wall_time_s": request["issued_wall_time_s"],
    }
    _require(
        all(_clock(x) for x in issued.values())
        and _fresh(obs, issued, scope)
        and not _expired(issued, scope),
        "human_deadline",
        "Human request was issued from stale or expired evidence",
    )
    return obs


def verify_tower_supervision(
    record, *, expected_scope, expected_observations=None, resolution_signing_key=None
):
    """Check closed record consistency and optional external controller-mode data."""
    result = {
        "schema": "missionos.starship_tower_supervision_verification.v1",
        "passed": False,
        "issues": [],
        "recorded_routing_request_count": 0,
        "recorded_routing_proposal_count": 0,
        "observation_collection_completed": False,
        "human_resolution_signature_valid": False,
        "directive_emitted": False,
        "later_application_report_consistent": False,
        "actual_guidance_mode_observed": False,
        "actual_guidance_mode_change_observed": False,
        "approval_independently_verified": False,
        "ledger_completeness_verified": False,
        "authenticated_human_independently_verified": False,
        "physical_effect_verified": False,
        "flight_outcome_improvement": False,
        "mission_completed": False,
        "model_value_established": False,
        "physical_execution": False,
        "ditch_trajectory_implemented": False,
    }
    try:
        _json(record)
        _json(expected_scope)
        _require(
            type(record) is dict
            and (set(record) == _LOG_FIELDS or set(record) == _LOG_FIELDS | {"events"})
            and record["schema"] == "missionos.starship_tower_supervision.v1",
            "record",
            "Expected closed tower supervision evidence",
        )
        scope = record["approved_scope"]
        _require(
            type(scope) is dict
            and set(scope) == _SCOPE_FIELDS
            and scope["schema"] == "missionos.starship_tower_supervision_scope.v1"
            and scope["scope"] == _SCOPE
            and _digest(scope) == _digest(expected_scope)
            and _hash(scope["source_sha256"])
            and _hash(scope["approval_record_sha256"]),
            "scope",
            "Record differs from the caller-bound approved source and plan",
        )
        _context(scope["context"])
        clocks = (
            "issued_simulation_time_s",
            "issued_wall_time_s",
            "original_simulation_deadline_s",
            "original_wall_deadline_s",
            "maximum_observation_age_s",
        )
        _require(
            all(_clock(scope[k]) for k in clocks)
            and 0
            < scope["original_simulation_deadline_s"] - scope["issued_simulation_time_s"]
            <= 1200
            and 0 < scope["original_wall_deadline_s"] - scope["issued_wall_time_s"] <= 1200
            and 0 < scope["maximum_observation_age_s"] <= 75
            and type(scope["observation_collection_allowed"]) is bool
            and type(scope["human_resolution_allowed"]) is bool,
            "scope",
            "Invalid approved source/deadline/observation scope",
        )
        _require(
            all(
                record[k] is False
                for k in (
                    "approval_independently_verified",
                    "physical_effect_verified",
                    "mission_completed",
                    "model_value_established",
                    "physical_execution",
                    "ditch_trajectory_implemented",
                )
            )
            and type(record["llm_calls"]) is int
            and record["llm_calls"] == 0,
            "claim_boundary",
            "Tower mode reports cannot claim flight improvement, human approval proof or LLM authority",
        )
        history = record["observations"]
        ticks = record["tick_inputs"]
        if "events" in record:
            _require(
                type(record["events"]) is list and 0 < len(record["events"]) <= 2048,
                "events",
                "Optional stale-resolution events require actual bounded rejections",
            )
        _require(
            type(history) is list
            and len(history) <= 2048
            and type(ticks) is list
            and len(ticks) <= 2048,
            "budget",
            "Tower observation/tick budget exceeded",
        )
        observations = {}
        last = None
        ids = set()
        for obs in history:
            _observation(obs, scope)
            _require(
                obs["observation_id"] not in ids
                and obs["evidence_sha256"] not in observations
                and (
                    last is None
                    or obs["simulation_time_s"] > last["simulation_time_s"]
                    and obs["wall_time_s"] >= last["wall_time_s"]
                ),
                "observation_order",
                "Duplicate or regressed observations cannot count as new sampling",
            )
            observations[obs["evidence_sha256"]] = obs
            ids.add(obs["observation_id"])
            last = obs
        prior = (scope["issued_simulation_time_s"], scope["issued_wall_time_s"])
        seen = []
        for tick in ticks:
            _require(
                type(tick) is dict
                and set(tick) == _TICK_FIELDS
                and all(_clock(tick[k]) for k in ("simulation_time_s", "wall_time_s"))
                and tick["simulation_time_s"] >= prior[0]
                and tick["wall_time_s"] >= prior[1]
                and type(tick["run_active"]) is bool
                and _hash(tick["source_sha256"])
                and _hash(tick["plan_sha256"])
                and all(
                    tick[k] is None or _hash(tick[k])
                    for k in ("routing_proposal_sha256", "human_decision_sha256")
                ),
                "tick",
                "Invalid caller clock, source, liveness or bounded input hash",
            )
            obs = observations.get(tick["observation_evidence_sha256"])
            _require(
                obs is not None
                and obs["simulation_time_s"] <= tick["simulation_time_s"]
                and obs["wall_time_s"] <= tick["wall_time_s"],
                "tick",
                "A tick did not retain its supplied actual observation",
            )
            if not seen or seen[-1] != obs["evidence_sha256"]:
                _require(
                    obs["evidence_sha256"] not in seen,
                    "tick",
                    "A prior observation was reused after a newer state",
                )
                seen.append(obs["evidence_sha256"])
            prior = tick["simulation_time_s"], tick["wall_time_s"]
        _require(
            seen == list(observations),
            "tick",
            "Observation history is not the complete tick input sequence",
        )
        requests = record["routing_requests"]
        proposals = record["routing_proposals"]
        _require(
            type(requests) is list
            and len(requests) <= 2
            and type(proposals) is list
            and len(proposals) <= len(requests),
            "routing_budget",
            "At most two routing calls are in the approved procedure",
        )
        request_map = {}
        proposal_map = {}
        for index, request in enumerate(requests, 1):
            _require(
                type(request) is dict
                and set(request) == _REQUEST_FIELDS
                and request["schema"] == "missionos.starship_tower_routing_request.v1"
                and request["context"] == scope["context"]
                and request["source_sha256"] == scope["source_sha256"]
                and request["approval_record_sha256"] == scope["approval_record_sha256"]
                and type(request["round"]) is int
                and request["round"] == index
                and request["allowed_routes"] == _ROUTES
                and request["allowed_actions"] == _ACTIONS
                and request["numeric_flight_authority"] is False
                and request["sha256"]
                == _digest({k: v for k, v in request.items() if k != "sha256"}),
                "routing_request",
                "Routing request widened or changed the approved scope",
            )
            _require(
                all(
                    request[k] == scope[k]
                    for k in (
                        "original_simulation_deadline_s",
                        "original_wall_deadline_s",
                        "maximum_observation_age_s",
                    )
                ),
                "routing_deadline",
                "Reassessment cannot extend the original deadline",
            )
            obs = observations.get(request["observation"]["evidence_sha256"])
            _require(
                obs == request["observation"] and request["sha256"] not in request_map,
                "routing_request",
                "Routing source state/evidence was replaced",
            )
            request_map[request["sha256"]] = request
        for proposal in proposals:
            _require(
                type(proposal) is dict
                and set(proposal) == _PROPOSAL_FIELDS
                and proposal["schema"] == "missionos.starship_tower_routing_proposal.v1"
                and proposal["request_sha256"] in request_map
                and proposal["context_sha256"] == _digest(scope["context"])
                and proposal["source_sha256"] == scope["source_sha256"]
                and proposal["observed_evidence_sha256"]
                == request_map[proposal["request_sha256"]]["observation"]["evidence_sha256"]
                and proposal["route"] in _ROUTES
                and proposal["proposed_action"] in (None, *_ACTIONS)
                and all(
                    proposal[k] is False
                    for k in (
                        "approval_granted",
                        "executor_command_issued",
                        "numeric_flight_authority",
                    )
                ),
                "routing_proposal",
                "Proposal did not retain its observation/source binding or narrow authority",
            )
            evidence = proposal["classifier_evidence"]
            _require(
                type(evidence) is dict
                and set(evidence)
                == {
                    "api",
                    "mode",
                    "model_inference_invoked",
                    "response_sha256",
                    "confidence_used_for_rules",
                    "llm_calls",
                }
                and evidence["api"] == "JevAssuranceJudge.judge"
                and evidence["mode"] in ("fixture", "live")
                and type(evidence["model_inference_invoked"]) is bool
                and (evidence["mode"] != "fixture" or evidence["model_inference_invoked"] is False)
                and (evidence["response_sha256"] is None or _hash(evidence["response_sha256"]))
                and (not evidence["model_inference_invoked"] or _hash(evidence["response_sha256"]))
                and evidence["confidence_used_for_rules"] is False
                and type(evidence["llm_calls"]) is int
                and evidence["llm_calls"] == 0,
                "classifier_evidence",
                "Routing evidence does not prove numeric authority or a model's value",
            )
            identity = _digest(proposal)
            _require(
                identity not in proposal_map
                and not any(
                    row["request_sha256"] == proposal["request_sha256"]
                    for row in proposal_map.values()
                ),
                "routing_budget",
                "A routing proposal was consumed twice",
            )
            proposal_map[identity] = proposal
        human = record["human_resolution"]
        human_request = human_decision = None
        if human is not None:
            _require(
                scope["human_resolution_allowed"]
                and type(human) is dict
                and set(human)
                == {"request", "decision", "llm_connected", "human_identity_independently_verified"}
                and human["llm_connected"] is False
                and human["human_identity_independently_verified"] is False,
                "human_scope",
                "Handover is not an additional model or execution authority",
            )
            human_request, human_decision = human["request"], human["decision"]
            _pending_hmac(human_request, scope, observations, resolution_signing_key)
            if human_decision is not None:
                decision_obs = observations.get(human_decision.get("observed_evidence_sha256"))
                _require(
                    decision_obs is not None
                    and verify_operator_resolution(
                        human_request,
                        human_decision,
                        signing_key=resolution_signing_key,
                        expected_context=scope["context"],
                        expected_latest_observation=_ledger_obs(decision_obs),
                        expected_run_active=True,
                    )["passed"],
                    "human_signature",
                    "Human choice is not the bound one-use signed decision",
                )
                result["human_resolution_signature_valid"] = True
        _replay(
            record,
            scope,
            ticks,
            observations,
            request_map,
            proposal_map,
            human_request,
            human_decision,
        )
        app = record["application_report"]
        if app is not None:
            result["later_application_report_consistent"] = True
            if expected_observations is not None:
                _json(expected_observations)
                _require(
                    type(expected_observations) is list and len(expected_observations) <= 2048,
                    "external_observation",
                    "Expected controller observations require a bounded separate saved list",
                )
                external = {}
                for obs in expected_observations:
                    _observation(obs, scope)
                    _require(
                        obs["evidence_sha256"] not in external,
                        "external_observation",
                        "Duplicate separate controller observation",
                    )
                    external[obs["evidence_sha256"]] = obs
                before = observations[record["directive"]["observed_evidence_sha256"]]
                after = observations[app["evidence_sha256"]]
                _require(
                    external.get(before["evidence_sha256"]) == before
                    and external.get(after["evidence_sha256"]) == after,
                    "external_observation",
                    "Application mode differs from separately stored actual controller observations",
                )
                result["actual_guidance_mode_observed"] = True
                result["actual_guidance_mode_change_observed"] = (
                    before["return_mode"] != after["return_mode"]
                )
        result.update(
            passed=True,
            recorded_routing_request_count=len(requests),
            recorded_routing_proposal_count=len(proposals),
            observation_collection_completed=record["observation_collection"] is not None
            and record["observation_collection"]["completed_evidence_sha256"] is not None,
            directive_emitted=record["directive"] is not None,
        )
    except _Invalid as exc:
        result["issues"].append(exc.issue)
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        RecursionError,
        AttributeError,
    ):
        result["issues"].append(
            {"code": "record", "detail": "Malformed tower-supervision evidence"}
        )
    return result


def _replay(record, scope, ticks, observations, requests, proposals, human_request, human_decision):
    """Reconstruct bounded causal choices from data; never execute a producer."""
    accepted = directive = collection = None
    pending_human = False
    current_request = None
    used_requests = set()
    used_proposals = set()
    human_seen = False
    expected_rules = []
    expected_events = []
    status = "observing"
    app = record["application_report"]
    app_seen = False

    def accept(action, source, obs, tick, binding=None):
        return {
            "action": action,
            "source": source,
            "simulation_time_s": tick["simulation_time_s"],
            "wall_time_s": tick["wall_time_s"],
            "evidence_sha256": obs["evidence_sha256"],
            "state_sha256": obs["state_sha256"],
            "return_mode_before": obs["return_mode"],
            "binding_sha256": binding,
            "accepted": True,
            "executor_command_issued": False,
            "numeric_flight_authority": False,
            "new_execution_authority_created": False,
        }

    def issue_request(obs, tick):
        candidates = [
            request
            for request in requests.values()
            if request["sha256"] not in used_requests
            and request["issued_simulation_time_s"] == tick["simulation_time_s"]
            and request["issued_wall_time_s"] == tick["wall_time_s"]
        ]
        _require(
            len(candidates) == 1,
            "routing_request",
            "An eligible routing tick lacks exactly one persisted request",
        )
        request = candidates[0]
        _require(
            request["round"] == len(used_requests) + 1 and request["observation"] == obs,
            "routing_request",
            "Routing sequence or actual observation is unbound",
        )
        used_requests.add(request["sha256"])
        return request

    def handover(obs, tick):
        nonlocal human_seen
        if not scope["human_resolution_allowed"]:
            _require(
                human_request is None,
                "human_scope",
                "Disabled human handover cannot create a transaction",
            )
            return False, "human_handover_unavailable"
        _require(
            human_request is not None
            and not human_seen
            and human_request["initial_observation"] == _ledger_obs(obs)
            and human_request["issued_simulation_time_s"] == tick["simulation_time_s"]
            and human_request["issued_wall_time_s"] == tick["wall_time_s"],
            "human_request",
            "Human handover is missing, duplicated or detached from its tick",
        )
        human_seen = True
        return True, "awaiting_human"

    for tick in ticks:
        obs = observations[tick["observation_evidence_sha256"]]
        good_source = (
            tick["source_sha256"] == scope["source_sha256"]
            and tick["plan_sha256"] == scope["context"]["plan_sha256"]
        )
        fresh = _fresh(obs, tick, scope)
        if not tick["run_active"]:
            status = "run_ended"
            continue
        if not good_source:
            status = "blocked_source_or_plan"
            continue
        if directive is not None:
            if (
                app is not None
                and app["evidence_sha256"] == obs["evidence_sha256"]
                and not app_seen
            ):
                _application(app, directive, accepted, obs, tick, scope)
                app_seen = True
                status = "application_reported"
            continue
        if accepted is not None:
            if not fresh or obs["simulation_time_s"] <= accepted["simulation_time_s"]:
                continue
            action, reason = accepted["action"], "accepted_choice_revalidated"
            if _expired(tick, scope):
                action, reason = "divert", "original_deadline_default_divert"
            elif obs["tower_ready"] is False:
                action, reason = "divert", "fresh_tower_unavailable_forces_divert"
            elif action == "continue_capture" and obs["tower_ready"] is not True:
                status = "accepted_waiting_fresh_readiness"
                continue
            directive = {
                "schema": "missionos.starship_tower_directive.v1",
                "context": scope["context"],
                "source_sha256": scope["source_sha256"],
                "approval_record_sha256": scope["approval_record_sha256"],
                "action": action,
                "reason": reason,
                "simulation_time_s": tick["simulation_time_s"],
                "wall_time_s": tick["wall_time_s"],
                "observed_evidence_sha256": obs["evidence_sha256"],
                "state_sha256": obs["state_sha256"],
                "consumed_once": True,
                "numeric_flight_authority": False,
                "requires_executor_rules_check": True,
                "executor_application_reported": False,
                "physical_effect_verified": False,
            }
            directive["sha256"] = _digest(directive)
            expected_rules.append(
                {
                    "evidence_sha256": obs["evidence_sha256"],
                    "action": action,
                    "reason": reason,
                    "simulation_time_s": tick["simulation_time_s"],
                    "wall_time_s": tick["wall_time_s"],
                    "allowed": True,
                }
            )
            status = "directive_emitted_waiting_application"
            continue
        if _expired(tick, scope):
            if pending_human:
                _require(
                    human_decision is not None,
                    "human_timeout",
                    "Expired pending human handover must retain its consumed or timeout transaction",
                )
            accepted = accept("divert", "original_deadline_fallback", obs, tick)
            status = "accepted_waiting_later_tick"
            continue
        if not fresh:
            status = "awaiting_fresh_observation"
            continue
        if obs["tower_ready"] is False:
            accepted = accept("divert", "deterministic_tower_unavailable", obs, tick)
            status = "accepted_waiting_later_tick"
            continue
        if tick["human_decision_sha256"] is not None:
            _require(
                pending_human
                and human_decision is not None
                and tick["human_decision_sha256"] == _digest(human_decision),
                "human_decision",
                "Human response is not the retained pending one-use transaction",
            )
            decision_obs = observations[human_decision["observed_evidence_sha256"]]
            _require(
                human_decision["simulation_time_s"] <= tick["simulation_time_s"]
                and human_decision["wall_time_s"] <= tick["wall_time_s"],
                "human_decision",
                "Received human decision is future at the actual consumption tick",
            )
            if not _fresh(decision_obs, tick, scope):
                expected_events.append(
                    {
                        "event": "stale_human_decision_rejected",
                        "decision_sha256": _digest(human_decision),
                        "decision_observed_evidence_sha256": decision_obs["evidence_sha256"],
                        "latest_observed_evidence_sha256": obs["evidence_sha256"],
                        "simulation_time_s": tick["simulation_time_s"],
                        "wall_time_s": tick["wall_time_s"],
                        "decision_observation_simulation_age_s": tick["simulation_time_s"]
                        - decision_obs["simulation_time_s"],
                        "decision_observation_wall_age_s": tick["wall_time_s"]
                        - decision_obs["wall_time_s"],
                        "maximum_observation_age_s": scope["maximum_observation_age_s"],
                        "request_slot_consumed": True,
                        "reapproval_created": False,
                        "executor_command_issued": False,
                    }
                )
                status = "awaiting_current_rules_after_stale_human"
                continue
            accepted = accept(
                human_decision["effective_action"],
                "operator_resolution",
                obs,
                tick,
                _digest(human_decision),
            )
            status = "accepted_waiting_later_tick"
            continue
        if pending_human:
            continue
        if collection is not None and collection["completed_evidence_sha256"] is None:
            if obs["simulation_time_s"] < collection["earliest_simulation_time_s"]:
                continue
            collection.update(
                completed_evidence_sha256=obs["evidence_sha256"],
                completed_state_sha256=obs["state_sha256"],
                completed_simulation_time_s=tick["simulation_time_s"],
                completed_wall_time_s=tick["wall_time_s"],
            )
            current_request = issue_request(obs, tick)
            status = "awaiting_routing"
            continue
        if tick["routing_proposal_sha256"] is not None:
            identity = tick["routing_proposal_sha256"]
            proposal = proposals.get(identity)
            _require(
                proposal is not None
                and identity not in used_proposals
                and current_request is not None
                and proposal["request_sha256"] == current_request["sha256"],
                "routing_proposal",
                "Consumed routing response is unknown, duplicated or belongs to a different request",
            )
            used_proposals.add(identity)
            route = proposal["route"]
            if route == "bounded" and proposal["proposed_action"] in _ACTIONS:
                if (
                    proposal["proposed_action"] == "continue_capture"
                    and obs["tower_ready"] is not True
                ):
                    route = "need_observation"
                else:
                    accepted = accept(
                        proposal["proposed_action"], "bounded_host_proposal", obs, tick, identity
                    )
                    status = "accepted_waiting_later_tick"
                    continue
            if (
                route == "need_observation"
                and scope["observation_collection_allowed"]
                and collection is None
            ):
                collection = {
                    "schema": "missionos.starship_tower_observation_request.v1",
                    "issued_evidence_sha256": obs["evidence_sha256"],
                    "issued_state_sha256": obs["state_sha256"],
                    "issued_simulation_time_s": tick["simulation_time_s"],
                    "issued_wall_time_s": tick["wall_time_s"],
                    "earliest_simulation_time_s": tick["simulation_time_s"] + 2.0,
                    "original_simulation_deadline_s": scope["original_simulation_deadline_s"],
                    "original_wall_deadline_s": scope["original_wall_deadline_s"],
                    "completed_evidence_sha256": None,
                    "actuator_operation": False,
                }
                status = "collecting_observation"
                continue
            pending_human, status = handover(obs, tick)
            continue
        if current_request is None:
            current_request = issue_request(obs, tick)
            status = "awaiting_routing"
    _require(
        used_requests == set(requests) and used_proposals == set(proposals),
        "routing_binding",
        "Unused or omitted routing records cannot create actions",
    )
    _require(
        human_seen is (human_request is not None),
        "human_binding",
        "Human request is not bound to its actual handover tick",
    )
    _require(
        _digest(record["accepted_decision"]) == _digest(accepted)
        and _digest(record["directive"]) == _digest(directive)
        and _digest(record["observation_collection"]) == _digest(collection)
        and _digest(record["rules"]) == _digest(expected_rules),
        "causal_replay",
        "Accepted choice, Rules, collection or directive differs from the causal bounded procedure",
    )
    _require(
        record.get("events", []) == expected_events,
        "events",
        "Stale human rejection differs from its actual signed decision, clocks or consumed request",
    )
    for event in record.get("events", []):
        _require(
            type(event) is dict
            and set(event) == set(expected_events[0])
            and event["request_slot_consumed"] is True
            and event["reapproval_created"] is False
            and event["executor_command_issued"] is False
            and all(
                _clock(event[k])
                for k in (
                    "simulation_time_s",
                    "wall_time_s",
                    "decision_observation_simulation_age_s",
                    "decision_observation_wall_age_s",
                    "maximum_observation_age_s",
                )
            ),
            "events",
            "Rejected old evidence cannot renew authority or issue an executor command",
        )
    _require(
        app is None or app_seen,
        "application",
        "Application report lacks a fresh later active/source-bound controller observation",
    )
    terminal = record["terminal_outcome"]
    if terminal is not None:
        _require(
            type(terminal) is dict
            and set(terminal)
            == {
                "state",
                "simulation_time_s",
                "wall_time_s",
                "termination",
                "outcome_sha256",
                "independent_verification_sha256",
            },
            "outcome",
            "Invalid closed terminal outcome report",
        )
        _state(terminal["state"])
        _require(
            terminal["simulation_time_s"] == terminal["state"]["time_s"]
            and _clock(terminal["wall_time_s"])
            and type(terminal["termination"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", terminal["termination"])
            and _hash(terminal["outcome_sha256"])
            and (
                terminal["independent_verification_sha256"] is None
                or _hash(terminal["independent_verification_sha256"])
            )
            and terminal["simulation_time_s"]
            >= (ticks[-1]["simulation_time_s"] if ticks else scope["issued_simulation_time_s"])
            and terminal["wall_time_s"]
            >= (ticks[-1]["wall_time_s"] if ticks else scope["issued_wall_time_s"]),
            "outcome",
            "Terminal outcome regressed or claims unbound terminal verification",
        )
        status = "finished"
    _require(
        record["status"] == status,
        "status",
        "Final supervision status contradicts the recorded causal inputs",
    )


def _application(report, directive, accepted, obs, tick, scope):
    fields = {
        "directive_sha256",
        "evidence_sha256",
        "state_sha256",
        "simulation_time_s",
        "wall_time_s",
        "observed_return_mode",
        "observed_guidance_mode_change",
        "executor_application_reported",
        "physical_effect_verified",
        "flight_outcome_improvement",
    }
    _require(
        type(report) is dict
        and set(report) == fields
        and report["directive_sha256"] == directive["sha256"]
        and report["evidence_sha256"] == obs["evidence_sha256"]
        and report["state_sha256"] == obs["state_sha256"]
        and report["simulation_time_s"] == obs["simulation_time_s"]
        and report["wall_time_s"] == obs["wall_time_s"]
        and obs["simulation_time_s"] > directive["simulation_time_s"]
        and obs["wall_time_s"] >= directive["wall_time_s"]
        and _fresh(obs, tick, scope)
        and report["observed_return_mode"] == obs["return_mode"]
        and obs["return_mode"]
        == ("capture" if directive["action"] == "continue_capture" else "divert")
        and report["observed_guidance_mode_change"]
        is (obs["return_mode"] != accepted["return_mode_before"])
        and report["executor_application_reported"] is True
        and report["physical_effect_verified"] is False
        and report["flight_outcome_improvement"] is False,
        "application",
        "Acceptance cannot replace a later actually observed mode or establish physical improvement",
    )

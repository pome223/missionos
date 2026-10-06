"""Typed tower-readiness supervision for an already approved local simulator.

Flight 6's published tower-health abort during boostback motivates a timely
readiness gate and a preplanned diversion. This local mechanism is not SpaceX
flight software, a validated diversion trajectory or evidence of LLM adoption.
The tick hook never changes physical state or issues numeric flight controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from threading import Lock

from .starship_operator_resolution import SCOPE, verify_operator_resolution

SCHEMA = "missionos.starship_tower_supervision.v1"
SCOPE_SCHEMA = "missionos.starship_tower_supervision_scope.v1"
OBSERVATION_SCHEMA = "missionos.starship_tower_observation.v1"
REQUEST_SCHEMA = "missionos.starship_tower_routing_request.v1"
PROPOSAL_SCHEMA = "missionos.starship_tower_routing_proposal.v1"
OPERATIONAL_REFERENCE = "https://www.spacex.com/launches/starship-flight-6"
ACTIONS = ("continue_capture", "divert")
ROUTES = ("bounded", "need_observation", "human_review", "deep_reasoning")
OBSERVATION_INTERVAL_S = 2.
MAX_OBSERVATIONS = 2048
_CONTEXT = {"session_id", "plan_id", "plan_sha256", "run_id", "request_id"}
_SCOPE_FIELDS = {"schema", "scope", "context", "approval_record_sha256", "source_sha256",
                 "issued_simulation_time_s", "issued_wall_time_s", "original_simulation_deadline_s",
                 "original_wall_deadline_s", "maximum_observation_age_s", "observation_collection_allowed",
                 "human_resolution_allowed"}
_OBSERVATION_FIELDS = {"schema", "context", "observation_id", "state", "state_sha256", "evidence_sha256",
                       "simulation_time_s", "wall_time_s", "tower_ready", "return_mode"}
_STATE_FIELDS = {"time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg",
                 "engine_states", "flap_angles_rad"}
_PROPOSAL_FIELDS = {"schema", "request_sha256", "context_sha256", "source_sha256", "observed_evidence_sha256",
                    "route", "proposed_action", "classifier_evidence", "approval_granted", "executor_command_issued",
                    "numeric_flight_authority"}
_REQUEST_FIELDS = {"schema", "context", "source_sha256", "approval_record_sha256", "round", "observation",
                   "issued_simulation_time_s", "issued_wall_time_s", "original_simulation_deadline_s",
                   "original_wall_deadline_s", "maximum_observation_age_s", "allowed_routes", "allowed_actions",
                   "numeric_flight_authority", "sha256"}


class TowerSupervisionError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise TowerSupervisionError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _snapshot(value):
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TowerSupervisionError("invalid_tower_json") from None


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _clock(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _context(value):
    _require(type(value) is dict and set(value) == _CONTEXT and _hash(value["plan_sha256"])
             and all(type(value[key]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value[key])
                     for key in ("session_id", "plan_id", "run_id", "request_id")), "invalid_tower_context")


def _scope(value):
    _require(type(value) is dict and set(value) == _SCOPE_FIELDS and value["schema"] == SCOPE_SCHEMA
             and value["scope"] == SCOPE and _hash(value["approval_record_sha256"])
             and _hash(value["source_sha256"]), "invalid_tower_scope")
    _context(value["context"])
    _require(all(_clock(value[key]) for key in ("issued_simulation_time_s", "issued_wall_time_s",
        "original_simulation_deadline_s", "original_wall_deadline_s", "maximum_observation_age_s"))
        and 0 < value["original_simulation_deadline_s"]-value["issued_simulation_time_s"] <= 1200.
        and 0 < value["original_wall_deadline_s"]-value["issued_wall_time_s"] <= 1200.
        and 0 < value["maximum_observation_age_s"] <= 75.
        and type(value["observation_collection_allowed"]) is bool and type(value["human_resolution_allowed"]) is bool,
        "invalid_tower_scope_deadlines")


def _state(value):
    _require(type(value) is dict and set(value) == _STATE_FIELDS and _clock(value["time_s"]), "invalid_tower_state")
    for key, length in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        vector = value[key]
        _require(type(vector) is list and len(vector) == length and all(type(x) in (int, float)
            and math.isfinite(x) and abs(x) <= 1e12 for x in vector), "invalid_tower_state")
    _require(abs(sum(x*x for x in value["q_body_to_eci"])-1.) <= 1e-8 and _clock(value["propellant_kg"])
             and type(value["engine_states"]) is list and 1 <= len(value["engine_states"]) <= 128
             and type(value["flap_angles_rad"]) is list and len(value["flap_angles_rad"]) <= 32
             and all(type(x) in (int, float) and math.isfinite(x) for x in value["flap_angles_rad"]), "invalid_tower_state")
    for engine in value["engine_states"]:
        _require(type(engine) is dict and set(engine) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
                 and type(engine["available"]) is bool and type(engine["throttle"]) in (int, float)
                 and 0 <= engine["throttle"] <= 1 and all(type(engine[key]) in (int, float) and math.isfinite(engine[key])
                 for key in ("gimbal_x_rad", "gimbal_y_rad")), "invalid_tower_state")


def _observation(value):
    _require(type(value) is dict and set(value) == _OBSERVATION_FIELDS and value["schema"] == OBSERVATION_SCHEMA,
             "invalid_tower_observation")
    _context(value["context"])
    _state(value["state"])
    _require(type(value["observation_id"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["observation_id"])
             and _hash(value["state_sha256"]) and value["state_sha256"] == _digest(value["state"])
             and _clock(value["simulation_time_s"]) and value["simulation_time_s"] == value["state"]["time_s"]
             and _clock(value["wall_time_s"]) and (value["tower_ready"] is None or type(value["tower_ready"]) is bool)
             and value["return_mode"] in ("capture", "divert", "undecided") and _hash(value["evidence_sha256"])
             and value["evidence_sha256"] == _digest({k: v for k, v in value.items() if k != "evidence_sha256"}),
             "tower_observation_binding_mismatch")


def tower_observation(*, context, observation_id, state, wall_time_s, tower_ready, return_mode):
    """Bind supplied actual state; the caller still owns sensor acquisition."""
    state, context = _snapshot(state), _snapshot(context)
    value = _snapshot({"schema": OBSERVATION_SCHEMA, "context": context, "observation_id": observation_id,
        "state": state, "state_sha256": _digest(state), "simulation_time_s": state["time_s"],
        "wall_time_s": wall_time_s, "tower_ready": tower_ready, "return_mode": return_mode})
    value["evidence_sha256"] = _digest(value)
    _observation(value)
    return value


def _ledger_observation(value):
    return {key: value[key] for key in ("evidence_sha256", "simulation_time_s", "wall_time_s", "tower_ready")}


@dataclass(frozen=True)
class TowerTick:
    routing_request: dict | None = None
    observation_request: dict | None = None
    human_request: dict | None = None
    directive: dict | None = None


class TowerSupervision:
    """One high-level choice; the executor and trajectory verifier stay external."""

    def __init__(self, approved_scope, *, resolution_ledger=None, resolution_signing_key=None):
        scope = _snapshot(approved_scope)
        _scope(scope)
        if scope["human_resolution_allowed"]:
            _require(resolution_ledger is not None and type(resolution_signing_key) is bytes
                     and 32 <= len(resolution_signing_key) <= 128, "tower_human_resolution_ledger_required")
        self._scope, self._ledger, self._key = scope, resolution_ledger, resolution_signing_key
        self._lock = Lock()
        self._last_clock = (scope["issued_simulation_time_s"], scope["issued_wall_time_s"])
        self._request, self._collection, self._human = None, None, None
        self._accepted, self._directive = None, None
        self._log = {"schema": SCHEMA, "approved_scope": scope, "observations": [], "routing_requests": [],
            "routing_proposals": [], "tick_inputs": [], "observation_collection": None, "human_resolution": None, "rules": [],
            "accepted_decision": None, "directive": None, "application_report": None, "terminal_outcome": None,
            "status": "observing", "approval_independently_verified": False, "physical_effect_verified": False,
            "mission_completed": False, "model_value_established": False, "physical_execution": False,
            "ditch_trajectory_implemented": False, "llm_calls": 0}

    def _fresh(self, observation, simulation, wall):
        age = self._scope["maximum_observation_age_s"]
        return 0 <= simulation-observation["simulation_time_s"] <= age and 0 <= wall-observation["wall_time_s"] <= age

    def _expired(self, simulation, wall):
        return simulation >= self._scope["original_simulation_deadline_s"] or wall >= self._scope["original_wall_deadline_s"]

    def _route_request(self, observation, simulation, wall):
        _require(len(self._log["routing_requests"]) < 2, "tower_routing_budget_exhausted")
        request = {"schema": REQUEST_SCHEMA, "context": self._scope["context"], "source_sha256": self._scope["source_sha256"],
            "approval_record_sha256": self._scope["approval_record_sha256"], "round": len(self._log["routing_requests"])+1,
            "observation": observation, "issued_simulation_time_s": simulation, "issued_wall_time_s": wall,
            "original_simulation_deadline_s": self._scope["original_simulation_deadline_s"],
            "original_wall_deadline_s": self._scope["original_wall_deadline_s"],
            "maximum_observation_age_s": self._scope["maximum_observation_age_s"], "allowed_routes": list(ROUTES),
            "allowed_actions": list(ACTIONS), "numeric_flight_authority": False}
        request = _snapshot(request)
        request["sha256"] = _digest(request)
        self._request = request
        self._log["routing_requests"].append(request)
        self._log["status"] = "awaiting_routing"
        return _snapshot(request)

    def _accept(self, action, source, observation, simulation, wall, *, binding_sha256=None):
        self._accepted = {"action": action, "source": source, "simulation_time_s": simulation, "wall_time_s": wall,
            "evidence_sha256": observation["evidence_sha256"], "state_sha256": observation["state_sha256"],
            "return_mode_before": observation["return_mode"], "binding_sha256": binding_sha256,
            "accepted": True, "executor_command_issued": False, "numeric_flight_authority": False,
            "new_execution_authority_created": False}
        self._log["accepted_decision"] = self._accepted
        self._log["status"] = "accepted_waiting_later_tick"

    def _handover(self, observation, simulation, wall):
        if not self._scope["human_resolution_allowed"]:
            self._log["status"] = "human_handover_unavailable"
            return None
        self._human = self._ledger.create_pending(context=self._scope["context"],
            initial_observation=_ledger_observation(observation), issued_simulation_time_s=simulation,
            issued_wall_time_s=wall, original_simulation_deadline_s=self._scope["original_simulation_deadline_s"],
            original_wall_deadline_s=self._scope["original_wall_deadline_s"], preapproved_scope=SCOPE,
            maximum_observation_age_s=self._scope["maximum_observation_age_s"])
        self._log["human_resolution"] = {"request": self._human, "decision": None,
            "llm_connected": False, "human_identity_independently_verified": False}
        self._log["status"] = "awaiting_human"
        return _snapshot(self._human)

    def tick(self, observation, *, simulation_time_s, wall_time_s, run_active, source_sha256, plan_sha256,
             routing_proposal=None, human_decision=None):
        """Consume fresh caller observations, never advance a simulator clock."""
        observation = _snapshot(observation)
        routing_proposal = _snapshot(routing_proposal) if routing_proposal is not None else None
        human_decision = _snapshot(human_decision) if human_decision is not None else None
        _observation(observation)
        with self._lock:
            _require(self._log["status"] != "finished", "tower_supervision_already_finished")
            _require(_clock(simulation_time_s) and _clock(wall_time_s) and simulation_time_s >= self._last_clock[0]
                     and wall_time_s >= self._last_clock[1] and observation["simulation_time_s"] <= simulation_time_s
                     and observation["wall_time_s"] <= wall_time_s, "tower_clock_regressed_or_future_observation")
            _require(observation["context"] == self._scope["context"], "tower_cross_context_observation")
            _require(type(run_active) is bool, "invalid_tower_run_liveness")
            _require(len(self._log["tick_inputs"]) < MAX_OBSERVATIONS and _hash(source_sha256)
                     and _hash(plan_sha256), "tower_tick_budget_or_source_invalid")
            history = self._log["observations"]
            if history and observation["observation_id"] == history[-1]["observation_id"]:
                _require(observation == history[-1], "tower_reused_observation_changed")
            else:
                _require(len(history) < MAX_OBSERVATIONS and (not history or observation["simulation_time_s"] > history[-1]["simulation_time_s"]
                         and observation["wall_time_s"] >= history[-1]["wall_time_s"]
                         and all(row["observation_id"] != observation["observation_id"] for row in history)), "tower_observation_order")
                history.append(observation)
            self._log["tick_inputs"].append({"observation_evidence_sha256": observation["evidence_sha256"],
                "simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s, "run_active": run_active,
                "source_sha256": source_sha256, "plan_sha256": plan_sha256,
                "routing_proposal_sha256": _digest(routing_proposal) if routing_proposal is not None else None,
                "human_decision_sha256": _digest(human_decision) if human_decision is not None else None})
            self._last_clock = simulation_time_s, wall_time_s
            if not run_active:
                self._log["status"] = "run_ended"
                return TowerTick()
            if source_sha256 != self._scope["source_sha256"] or plan_sha256 != self._scope["context"]["plan_sha256"]:
                self._log["status"] = "blocked_source_or_plan"
                return TowerTick()
            fresh = self._fresh(observation, simulation_time_s, wall_time_s)
            if self._directive is not None or self._log["status"] in ("application_reported", "finished"):
                return TowerTick()
            if self._accepted is not None:
                if not fresh or observation["simulation_time_s"] <= self._accepted["simulation_time_s"]:
                    return TowerTick()
                action, reason = self._accepted["action"], "accepted_choice_revalidated"
                if self._expired(simulation_time_s, wall_time_s):
                    action, reason = "divert", "original_deadline_default_divert"
                elif observation["tower_ready"] is False:
                    action, reason = "divert", "fresh_tower_unavailable_forces_divert"
                elif action == "continue_capture" and observation["tower_ready"] is not True:
                    self._log["status"] = "accepted_waiting_fresh_readiness"
                    return TowerTick()
                directive = {"schema": "missionos.starship_tower_directive.v1", "context": self._scope["context"],
                    "source_sha256": source_sha256, "approval_record_sha256": self._scope["approval_record_sha256"],
                    "action": action, "reason": reason, "simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s,
                    "observed_evidence_sha256": observation["evidence_sha256"], "state_sha256": observation["state_sha256"],
                    "consumed_once": True, "numeric_flight_authority": False, "requires_executor_rules_check": True,
                    "executor_application_reported": False, "physical_effect_verified": False}
                directive = _snapshot(directive)
                directive["sha256"] = _digest(directive)
                self._directive = directive
                self._log["directive"] = directive
                self._log["rules"].append({"evidence_sha256": observation["evidence_sha256"], "action": action,
                    "reason": reason, "simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s, "allowed": True})
                self._log["status"] = "directive_emitted_waiting_application"
                return TowerTick(directive=_snapshot(directive))
            if self._expired(simulation_time_s, wall_time_s):
                if self._human is not None:
                    decision = self._ledger.expire_or_get(context=self._scope["context"], request_sha256=self._human["sha256"],
                        simulation_time_s=simulation_time_s, wall_time_s=wall_time_s, run_active=True,
                        latest_observation=_ledger_observation(observation))
                    self._log["human_resolution"]["decision"] = decision
                self._accept("divert", "original_deadline_fallback", observation, simulation_time_s, wall_time_s)
                return TowerTick()
            if not fresh:
                self._log["status"] = "awaiting_fresh_observation"
                return TowerTick()
            if observation["tower_ready"] is False:
                self._accept("divert", "deterministic_tower_unavailable", observation, simulation_time_s, wall_time_s)
                return TowerTick()
            if human_decision is not None:
                _require(self._human is not None and self._accepted is None, "no_pending_tower_human_request")
                records = self._ledger.records(self._scope["context"])
                decision_observation = next((row for row in history
                    if row["evidence_sha256"] == human_decision.get("observed_evidence_sha256")), None)
                _require(records["request"] == self._human and records["decision"] == human_decision
                    and decision_observation is not None
                    and verify_operator_resolution(self._human, human_decision, signing_key=self._key,
                        expected_context=self._scope["context"], expected_latest_observation=_ledger_observation(decision_observation),
                        expected_run_active=True)["passed"] and human_decision["simulation_time_s"] <= simulation_time_s
                    and human_decision["wall_time_s"] <= wall_time_s, "unbound_tower_human_decision")
                self._log["human_resolution"]["decision"] = human_decision
                if not self._fresh(decision_observation, simulation_time_s, wall_time_s):
                    self._log.setdefault("events", []).append({"event": "stale_human_decision_rejected",
                        "decision_sha256": _digest(human_decision),
                        "decision_observed_evidence_sha256": decision_observation["evidence_sha256"],
                        "latest_observed_evidence_sha256": observation["evidence_sha256"],
                        "simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s,
                        "decision_observation_simulation_age_s": simulation_time_s-decision_observation["simulation_time_s"],
                        "decision_observation_wall_age_s": wall_time_s-decision_observation["wall_time_s"],
                        "maximum_observation_age_s": self._scope["maximum_observation_age_s"],
                        "request_slot_consumed": True, "reapproval_created": False, "executor_command_issued": False})
                    self._log["status"] = "awaiting_current_rules_after_stale_human"
                    return TowerTick()
                self._accept(human_decision["effective_action"], "operator_resolution", observation, simulation_time_s,
                             wall_time_s, binding_sha256=_digest(human_decision))
                return TowerTick()
            if self._human is not None:
                return TowerTick()
            if self._collection is not None and self._collection["completed_evidence_sha256"] is None:
                if observation["simulation_time_s"] < self._collection["earliest_simulation_time_s"]:
                    return TowerTick()
                self._collection.update(completed_evidence_sha256=observation["evidence_sha256"],
                    completed_state_sha256=observation["state_sha256"], completed_simulation_time_s=simulation_time_s,
                    completed_wall_time_s=wall_time_s)
                return TowerTick(routing_request=self._route_request(observation, simulation_time_s, wall_time_s))
            if routing_proposal is not None:
                _require(self._request is not None and set(routing_proposal) == _PROPOSAL_FIELDS
                    and routing_proposal["schema"] == PROPOSAL_SCHEMA and routing_proposal["request_sha256"] == self._request["sha256"]
                    and routing_proposal["context_sha256"] == _digest(self._scope["context"])
                    and routing_proposal["source_sha256"] == self._scope["source_sha256"]
                    and routing_proposal["observed_evidence_sha256"] == self._request["observation"]["evidence_sha256"]
                    and routing_proposal["route"] in ROUTES and routing_proposal["proposed_action"] in (None, *ACTIONS)
                    and routing_proposal["approval_granted"] is False and routing_proposal["executor_command_issued"] is False
                    and routing_proposal["numeric_flight_authority"] is False,
                    "unbound_tower_routing_proposal")
                evidence = routing_proposal["classifier_evidence"]
                _require(type(evidence) is dict and set(evidence) == {"api", "mode", "model_inference_invoked",
                    "response_sha256", "confidence_used_for_rules", "llm_calls"}
                    and evidence["api"] == "JevAssuranceJudge.judge" and evidence["mode"] in ("fixture", "live")
                    and type(evidence["model_inference_invoked"]) is bool
                    and (evidence["mode"] != "fixture" or evidence["model_inference_invoked"] is False)
                    and (evidence["response_sha256"] is None or _hash(evidence["response_sha256"]))
                    and evidence["confidence_used_for_rules"] is False and type(evidence["llm_calls"]) is int
                    and evidence["llm_calls"] == 0, "invalid_tower_classifier_evidence")
                _require(not any(row["request_sha256"] == routing_proposal["request_sha256"] for row in self._log["routing_proposals"]),
                         "tower_routing_proposal_already_used")
                self._log["routing_proposals"].append(routing_proposal)
                route = routing_proposal["route"]
                if route == "bounded" and routing_proposal["proposed_action"] in ACTIONS:
                    if routing_proposal["proposed_action"] == "continue_capture" and observation["tower_ready"] is not True:
                        route = "need_observation"
                    else:
                        self._accept(routing_proposal["proposed_action"], "bounded_host_proposal", observation,
                                     simulation_time_s, wall_time_s, binding_sha256=_digest(routing_proposal))
                        return TowerTick()
                if route == "need_observation" and self._scope["observation_collection_allowed"] and self._collection is None:
                    self._collection = {"schema": "missionos.starship_tower_observation_request.v1",
                        "issued_evidence_sha256": observation["evidence_sha256"], "issued_state_sha256": observation["state_sha256"],
                        "issued_simulation_time_s": simulation_time_s, "issued_wall_time_s": wall_time_s,
                        "earliest_simulation_time_s": simulation_time_s+OBSERVATION_INTERVAL_S,
                        "original_simulation_deadline_s": self._scope["original_simulation_deadline_s"],
                        "original_wall_deadline_s": self._scope["original_wall_deadline_s"],
                        "completed_evidence_sha256": None, "actuator_operation": False}
                    self._log["observation_collection"] = self._collection
                    self._log["status"] = "collecting_observation"
                    return TowerTick(observation_request=_snapshot(self._collection))
                return TowerTick(human_request=self._handover(observation, simulation_time_s, wall_time_s))
            if self._request is None:
                return TowerTick(routing_request=self._route_request(observation, simulation_time_s, wall_time_s))
            return TowerTick()

    def record_application(self, *, directive_sha256, observed_evidence_sha256):
        """Record an executor report against a later already received observation."""
        with self._lock:
            _require(self._directive is not None and directive_sha256 == self._directive["sha256"]
                     and self._log["application_report"] is None, "tower_application_not_pending")
            observation = self._log["observations"][-1]
            _require(observed_evidence_sha256 == observation["evidence_sha256"]
                     and observation["simulation_time_s"] > self._directive["simulation_time_s"]
                     and observation["wall_time_s"] >= self._directive["wall_time_s"]
                     and self._fresh(observation, *self._last_clock)
                     and observation["return_mode"] == ("capture" if self._directive["action"] == "continue_capture" else "divert")
                     and self._log["status"] != "run_ended" and self._log["status"] != "blocked_source_or_plan",
                     "tower_application_lacks_later_mode_observation")
            report = {"directive_sha256": directive_sha256, "evidence_sha256": observed_evidence_sha256,
                "state_sha256": observation["state_sha256"], "simulation_time_s": observation["simulation_time_s"],
                "wall_time_s": observation["wall_time_s"], "observed_return_mode": observation["return_mode"],
                "observed_guidance_mode_change": observation["return_mode"] != self._accepted["return_mode_before"],
                "executor_application_reported": True, "physical_effect_verified": False, "flight_outcome_improvement": False}
            self._log["application_report"] = report
            self._log["status"] = "application_reported"
            return _snapshot(report)

    def finish(self, outcome):
        outcome = _snapshot(outcome)
        _require(type(outcome) is dict and set(outcome) == {"state", "simulation_time_s", "wall_time_s", "termination",
            "outcome_sha256", "independent_verification_sha256"}, "invalid_tower_outcome")
        _state(outcome["state"])
        _require(outcome["simulation_time_s"] == outcome["state"]["time_s"] and _clock(outcome["wall_time_s"])
            and type(outcome["termination"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", outcome["termination"])
            and _hash(outcome["outcome_sha256"]) and (outcome["independent_verification_sha256"] is None
            or _hash(outcome["independent_verification_sha256"])), "invalid_tower_outcome")
        with self._lock:
            _require(self._log["terminal_outcome"] is None and outcome["simulation_time_s"] >= self._last_clock[0]
                     and outcome["wall_time_s"] >= self._last_clock[1], "tower_outcome_order")
            self._log["terminal_outcome"] = outcome
            self._log["status"] = "finished"
            return _snapshot(self._log)

    def receipt(self):
        with self._lock:
            return _snapshot(self._log)


class TowerRoutingAdapter:
    """Host-only two-call adapter for the existing Jev `.judge(prompt)` API."""

    def __init__(self, judge, *, mode="fixture"):
        _require(mode in ("fixture", "live") and callable(getattr(judge, "judge", None)), "invalid_tower_classifier")
        _require(mode != "fixture" or getattr(judge, "fixture_only", None) is True,
                 "tower_fixture_judge_declaration_required")
        self._judge, self._mode, self._used, self._lock = judge, mode, set(), Lock()

    def assess(self, request, *, run_active, source_current):
        request = _snapshot(request)
        _require(type(request) is dict and set(request) == _REQUEST_FIELDS and request.get("schema") == REQUEST_SCHEMA
                 and _hash(request.get("sha256")) and request["sha256"] == _digest({k: v for k, v in request.items() if k != "sha256"})
                 and run_active is True and source_current is True, "invalid_tower_routing_request")
        _observation(request["observation"])
        _context(request["context"])
        _require(request["context"] == request["observation"]["context"] and _hash(request["source_sha256"])
            and _hash(request["approval_record_sha256"]) and type(request["round"]) is int and request["round"] in (1, 2)
            and request["allowed_routes"] == list(ROUTES) and request["allowed_actions"] == list(ACTIONS)
            and request["numeric_flight_authority"] is False
            and all(_clock(request[key]) for key in ("issued_simulation_time_s", "issued_wall_time_s",
                "original_simulation_deadline_s", "original_wall_deadline_s", "maximum_observation_age_s"))
            and request["issued_simulation_time_s"] < request["original_simulation_deadline_s"]
            and request["issued_wall_time_s"] < request["original_wall_deadline_s"]
            and 0 < request["maximum_observation_age_s"] <= 75.
            and 0 <= request["issued_simulation_time_s"]-request["observation"]["simulation_time_s"] <= request["maximum_observation_age_s"]
            and 0 <= request["issued_wall_time_s"]-request["observation"]["wall_time_s"] <= request["maximum_observation_age_s"],
            "invalid_tower_routing_request")
        with self._lock:
            _require(len(self._used) < 2 and request["sha256"] not in self._used, "tower_classifier_budget_exhausted")
            self._used.add(request["sha256"])
        # The existing bounded classifier uses these neutral prompt fields.
        # Correlation IDs and raw full physical state are not provider inputs.
        observation = request["observation"]
        semantics = {"continue": "Propose continuing the already approved capture path only if fresh tower readiness is true.",
            "replan": "Propose the already approved local simulation diversion; no trajectory or safety claim.",
            "operator_escalation": "An observation or human review is needed before proposing either bounded option."}
        prompt = {"schema_version": "missionos_mission_assurance_prompt.v1",
            "mission_situation": {"mission_contract": {"response_mapping": semantics},
                "observations": {key: observation[key] for key in ("simulation_time_s", "wall_time_s", "tower_ready", "return_mode")},
                "constraints": {"original_simulation_deadline_s": request["original_simulation_deadline_s"],
                    "original_wall_deadline_s": request["original_wall_deadline_s"], "numeric_flight_authority": False},
                "execution_scope": "local_sixdof_development_simulator_only"},
            "decision_contract": {"allowed_response_kinds": list(semantics)}, "response_semantics": semantics}
        route, action, inference, response_digest = "human_review", None, False, None
        try:
            judgment = self._judge.judge(prompt)
            output, evidence = _snapshot(judgment.output), _snapshot(judgment.invocation_evidence)
            route = evidence.get("assessment_route", "human_review")
            _require(route in ROUTES and output.get("parameters") == {}, "invalid_tower_classifier_response")
            action = {"continue": "continue_capture", "replan": "divert", "operator_escalation": None}.get(output.get("proposed_response_kind"))
            if route != "bounded":
                action = None
            inference = self._mode == "live" and judgment.model_inference_invoked is True
            response_digest = _digest({"output": output, "evidence": evidence})
        except Exception:
            route, action, inference = "human_review", None, False
        return {"schema": PROPOSAL_SCHEMA, "request_sha256": request["sha256"], "context_sha256": _digest(request["context"]),
            "source_sha256": request["source_sha256"], "observed_evidence_sha256": observation["evidence_sha256"],
            "route": route, "proposed_action": action, "classifier_evidence": {"api": "JevAssuranceJudge.judge",
                "mode": self._mode, "model_inference_invoked": inference, "response_sha256": response_digest,
                "confidence_used_for_rules": False, "llm_calls": 0}, "approval_granted": False,
            "executor_command_issued": False, "numeric_flight_authority": False}

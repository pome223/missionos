"""One-use operator choice ledger for a preapproved local simulator scope.

This module does not authenticate a person, run a provider, read credentials,
command an actuator or verify a flight outcome. The caller supplies clocks,
current observations, run liveness and its local HMAC key. Executor Rules must
still check the actual physical corridor, pins, fuel and tower before acting.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock

REQUEST_SCHEMA = "missionos.starship_operator_resolution_request.v1"
DECISION_SCHEMA = "missionos.starship_operator_resolution_decision.v1"
SCOPE = "preapproved_local_simulation_capture_or_divert_v1"
DEFAULT_ACTIONS = ("continue_capture", "divert")
_CONTEXT_FIELDS = {"session_id", "plan_id", "plan_sha256", "run_id", "request_id"}
_OBSERVATION_FIELDS = {"evidence_sha256", "simulation_time_s", "wall_time_s", "tower_ready"}
_REQUEST_FIELDS = {"schema", "scope", "context", "observed_evidence_sha256", "initial_observation",
    "allowed_actions", "issued_simulation_time_s", "issued_wall_time_s", "original_simulation_deadline_s",
    "original_wall_deadline_s", "maximum_observation_age_s", "simulation_only", "numeric_flight_authority",
    "limits_waivable", "sha256", "signature"}
_DECISION_FIELDS = {"schema", "scope", "context", "request_sha256", "request_observed_evidence_sha256",
    "observed_evidence_sha256", "latest_observation", "source", "requested_action", "effective_action", "reason",
    "simulation_time_s", "wall_time_s", "run_active", "human_identity_authenticated", "operator_response_received",
    "request_slot_consumed", "requires_executor_rules_check", "execution_authority_created", "executor_command_issued",
    "observed_effect_verified", "mission_completed", "physical_execution", "numeric_flight_authority", "limits_waived",
    "model_value_established", "signature"}


class OperatorResolutionError(ValueError):
    """A fixed reason, without caller data or secrets in its message."""


def _require(condition, reason):
    if not condition:
        raise OperatorResolutionError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _hash(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _context(value):
    _require(type(value) is dict and set(value) == _CONTEXT_FIELDS, "invalid_resolution_context")
    _require(_hash(value["plan_sha256"]) and all(type(value[k]) is str and
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value[k]) is not None
        for k in ("session_id", "plan_id", "run_id", "request_id")), "invalid_resolution_context")


def _observation(value):
    _require(type(value) is dict and set(value) == _OBSERVATION_FIELDS, "invalid_resolution_observation")
    _require(_hash(value["evidence_sha256"]) and _number(value["simulation_time_s"])
             and _number(value["wall_time_s"]) and (value["tower_ready"] is None or type(value["tower_ready"]) is bool),
             "invalid_resolution_observation")


def _key(value):
    _require(type(value) is bytes and 32 <= len(value) <= 128, "invalid_resolution_signing_key")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _copy(value):
    return json.loads(_canonical(value))


def _snapshot(value, reason):
    _require(type(value) is dict, reason)
    try:
        return _copy(value)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise OperatorResolutionError(reason) from None


def _signature(value, key):
    return hmac.new(key, _canonical({k: v for k, v in value.items() if k != "signature"}), "sha256").hexdigest()


def _clock(request, simulation_time_s, wall_time_s):
    _require(_number(simulation_time_s) and _number(wall_time_s)
             and simulation_time_s >= request["issued_simulation_time_s"]
             and wall_time_s >= request["issued_wall_time_s"], "resolution_clock_regressed")


def _expired(request, simulation_time_s, wall_time_s):
    return (simulation_time_s >= request["original_simulation_deadline_s"]
            or wall_time_s >= request["original_wall_deadline_s"])


def _evidence_time(request, observation, simulation_time_s, wall_time_s, *, fresh):
    initial = request["initial_observation"]
    _require(initial["simulation_time_s"] <= observation["simulation_time_s"] <= simulation_time_s
             and initial["wall_time_s"] <= observation["wall_time_s"] <= wall_time_s,
             "resolution_observation_clock_mismatch")
    if fresh:
        age = request["maximum_observation_age_s"]
        _require(simulation_time_s-observation["simulation_time_s"] <= age
                 and wall_time_s-observation["wall_time_s"] <= age, "resolution_observation_stale")


def _request(value, key):
    _require(type(value) is dict and set(value) == _REQUEST_FIELDS, "invalid_resolution_request")
    _context(value["context"])
    _observation(value["initial_observation"])
    _require(type(value["schema"]) is str and type(value["scope"]) is str
             and value["schema"] == REQUEST_SCHEMA and value["scope"] == SCOPE
             and value["simulation_only"] is True and value["numeric_flight_authority"] is False
             and value["limits_waivable"] is False, "invalid_resolution_scope")
    actions = value["allowed_actions"]
    _require(type(actions) is list and 1 <= len(actions) <= 2
             and all(type(a) is str and a in DEFAULT_ACTIONS for a in actions)
             and len(set(actions)) == len(actions) and "divert" in actions, "invalid_resolution_actions")
    _require(_hash(value["observed_evidence_sha256"])
             and value["observed_evidence_sha256"] == value["initial_observation"]["evidence_sha256"],
             "resolution_evidence_binding_mismatch")
    for name in ("issued_simulation_time_s", "issued_wall_time_s", "original_simulation_deadline_s", "original_wall_deadline_s"):
        _require(_number(value[name]), "invalid_resolution_clock")
    _require(0 < value["original_simulation_deadline_s"]-value["issued_simulation_time_s"] <= 1200
             and 0 < value["original_wall_deadline_s"]-value["issued_wall_time_s"] <= 1200
             and _number(value["maximum_observation_age_s"]) and 0 < value["maximum_observation_age_s"] <= 75.,
             "invalid_original_resolution_deadline")
    _evidence_time(value, value["initial_observation"], value["issued_simulation_time_s"], value["issued_wall_time_s"], fresh=True)
    unsigned = {k: v for k, v in value.items() if k not in {"sha256", "signature"}}
    _require(_hash(value["sha256"]) and value["sha256"] == sha256(_canonical(unsigned)).hexdigest(),
             "resolution_request_hash_mismatch")
    _require(_hash(value["signature"]) and hmac.compare_digest(value["signature"], _signature(value, key)),
             "resolution_request_signature_mismatch")


def _decision(request, observation, action, source, simulation_time_s, wall_time_s, human_authenticated, key):
    if source == "timeout_fallback":
        effective, reason = "divert", "original_deadline_expired"
    elif observation["tower_ready"] is False:
        effective, reason = "divert", "tower_unavailable_forces_divert"
    elif action == "continue_capture":
        _require(observation["tower_ready"] is True, "tower_readiness_unknown")
        effective, reason = "continue_capture", "fresh_tower_ready"
    else:
        effective, reason = "divert", "operator_selected_divert"
    result = {"schema": DECISION_SCHEMA, "scope": SCOPE, "context": _copy(request["context"]),
        "request_sha256": request["sha256"], "request_observed_evidence_sha256": request["observed_evidence_sha256"],
        "observed_evidence_sha256": observation["evidence_sha256"], "latest_observation": _copy(observation),
        "source": source, "requested_action": action, "effective_action": effective, "reason": reason,
        "simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s, "run_active": True,
        "human_identity_authenticated": human_authenticated, "operator_response_received": source == "operator",
        "request_slot_consumed": True, "requires_executor_rules_check": True,
        "execution_authority_created": False, "executor_command_issued": False, "observed_effect_verified": False,
        "mission_completed": False, "physical_execution": False, "numeric_flight_authority": False,
        "limits_waived": False, "model_value_established": False}
    result["signature"] = _signature(result, key)
    return result


class OperatorResolutionLedger:
    """At most one signed choice per run; callers cannot mutate retained data.

    Optional SQLite storage preserves the same transaction boundary across
    cooperating processes and restarts. The isolated path and signing key are
    caller supplied. Signatures detect altered records; this is not a proof
    against deletion or rollback of a database by its filesystem owner.
    """

    def __init__(self, signing_key, *, store_path=None):
        _key(signing_key)
        self._signing_key = signing_key
        self._lock = Lock()
        self._requests = {}
        self._decisions = {}
        self._runs = {}
        self._store_path = None
        if store_path is not None:
            _require(isinstance(store_path, (str, os.PathLike)), "invalid_resolution_store_path")
            path = os.fspath(store_path)
            _require(type(path) is str and path.strip() and path != ":memory:" and "\0" not in path,
                     "invalid_resolution_store_path")
            self._store_path = str(Path(path).absolute())
            with self._transaction() as database:
                database.execute("CREATE TABLE IF NOT EXISTS operator_resolution_metadata "
                    "(version INTEGER PRIMARY KEY CHECK (version = 1), key_check TEXT NOT NULL)")
                database.execute("CREATE TABLE IF NOT EXISTS operator_resolution_requests "
                    "(request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, "
                    "request_json TEXT NOT NULL, decision_json TEXT)")
                key_check = hmac.new(signing_key, b"missionos.starship_operator_resolution_store.v1", "sha256").hexdigest()
                retained = database.execute("SELECT version, key_check FROM operator_resolution_metadata").fetchall()
                if not retained:
                    database.execute("INSERT INTO operator_resolution_metadata VALUES (1, ?)", (key_check,))
                else:
                    _require(len(retained) == 1 and retained[0][0] == 1 and type(retained[0][1]) is str
                             and hmac.compare_digest(retained[0][1], key_check), "resolution_store_key_mismatch")

    @contextmanager
    def _transaction(self):
        """Do not retain a connection across threads, forks or caller requests."""
        with self._lock:
            database = None
            try:
                if self._store_path is not None:
                    database = sqlite3.connect(self._store_path, timeout=5., isolation_level=None)
                    database.execute("PRAGMA synchronous = FULL")
                    database.execute("BEGIN IMMEDIATE")
                yield database
                if database is not None:
                    database.commit()
            except sqlite3.Error:
                if database is not None:
                    database.rollback()
                raise OperatorResolutionError("resolution_store_unavailable") from None
            except BaseException:
                if database is not None:
                    database.rollback()
                raise
            finally:
                if database is not None:
                    database.close()

    def _load(self, request_id, database):
        if database is None:
            return self._requests.get(request_id), self._decisions.get(request_id)
        row = database.execute("SELECT run_id, request_json, decision_json FROM "
                               "operator_resolution_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None, None
        try:
            request = json.loads(row[1])
            _request(request, self._signing_key)
            _require(request["context"]["request_id"] == request_id and request["context"]["run_id"] == row[0],
                     "resolution_store_record_binding_mismatch")
            decision = None if row[2] is None else json.loads(row[2])
            if decision is not None:
                _require(type(decision) is dict and "latest_observation" in decision
                         and verify_operator_resolution(request, decision, signing_key=self._signing_key,
                             expected_context=request["context"], expected_latest_observation=decision["latest_observation"],
                             expected_run_active=True)["passed"], "resolution_store_decision_invalid")
            return request, decision
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise OperatorResolutionError("resolution_store_record_invalid") from None

    def _retain_decision(self, request, decision, database):
        request_id = request["context"]["request_id"]
        if database is None:
            self._decisions[request_id] = decision
        else:
            changed = database.execute("UPDATE operator_resolution_requests SET decision_json = ? "
                "WHERE request_id = ? AND decision_json IS NULL", (_canonical(decision).decode(), request_id)).rowcount
            _require(changed == 1, "resolution_already_consumed")

    def create_pending(self, *, context, initial_observation, issued_simulation_time_s, issued_wall_time_s,
                       original_simulation_deadline_s, original_wall_deadline_s, preapproved_scope,
                       allowed_actions=DEFAULT_ACTIONS, maximum_observation_age_s=2.):
        context = _snapshot(context, "invalid_resolution_context")
        initial_observation = _snapshot(initial_observation, "invalid_resolution_observation")
        _context(context)
        _observation(initial_observation)
        _require(type(preapproved_scope) is str and preapproved_scope == SCOPE, "preapproved_simulation_scope_required")
        _require(type(allowed_actions) in (list, tuple), "invalid_resolution_actions")
        request = {"schema": REQUEST_SCHEMA, "scope": SCOPE, "context": _copy(context),
            "observed_evidence_sha256": initial_observation["evidence_sha256"], "initial_observation": _copy(initial_observation),
            "allowed_actions": list(allowed_actions), "issued_simulation_time_s": issued_simulation_time_s,
            "issued_wall_time_s": issued_wall_time_s, "original_simulation_deadline_s": original_simulation_deadline_s,
            "original_wall_deadline_s": original_wall_deadline_s, "maximum_observation_age_s": maximum_observation_age_s,
            "simulation_only": True, "numeric_flight_authority": False, "limits_waivable": False}
        try:
            request["sha256"] = sha256(_canonical(request)).hexdigest()
            request["signature"] = _signature(request, self._signing_key)
            _request(request, self._signing_key)
        except (ValueError, TypeError):
            raise OperatorResolutionError("invalid_pending_resolution_request") from None
        with self._transaction() as database:
            retained = request["context"]
            if database is None:
                _require(retained["request_id"] not in self._requests, "resolution_request_already_created")
                _require(retained["run_id"] not in self._runs, "resolution_run_budget_exhausted")
                self._requests[retained["request_id"]] = request
                self._runs[retained["run_id"]] = retained["request_id"]
            else:
                _require(database.execute("SELECT 1 FROM operator_resolution_requests WHERE request_id = ?",
                         (retained["request_id"],)).fetchone() is None, "resolution_request_already_created")
                _require(database.execute("SELECT 1 FROM operator_resolution_requests WHERE run_id = ?",
                         (retained["run_id"],)).fetchone() is None, "resolution_run_budget_exhausted")
                database.execute("INSERT INTO operator_resolution_requests VALUES (?, ?, ?, NULL)",
                    (retained["request_id"], retained["run_id"], _canonical(request).decode()))
        return _copy(request)

    def _bound(self, context, request_sha256, database=None):
        _context(context)
        request, decision = self._load(context["request_id"], database)
        _require(request is not None, "no_pending_resolution_request")
        _require(_hash(request_sha256) and context == request["context"] and request_sha256 == request["sha256"],
                 "resolution_context_binding_mismatch")
        _require(decision is None, "resolution_already_consumed")
        return request

    def resolve(self, *, context, request_sha256, observed_evidence_sha256, latest_observation, action,
                simulation_time_s, wall_time_s, run_active, human_identity_authenticated=False):
        context = _snapshot(context, "invalid_resolution_context")
        latest_observation = _snapshot(latest_observation, "invalid_resolution_observation")
        with self._transaction() as database:
            request = self._bound(context, request_sha256, database)
            _require(type(run_active) is bool and run_active, "resolution_run_not_active")
            _require(type(human_identity_authenticated) is bool, "invalid_operator_identity_flag")
            _clock(request, simulation_time_s, wall_time_s)
            _require(not _expired(request, simulation_time_s, wall_time_s), "resolution_deadline_expired")
            _observation(latest_observation)
            _require(_hash(observed_evidence_sha256) and observed_evidence_sha256 == latest_observation["evidence_sha256"],
                     "resolution_evidence_binding_mismatch")
            _evidence_time(request, latest_observation, simulation_time_s, wall_time_s, fresh=True)
            _require(type(action) is str and action in request["allowed_actions"], "resolution_action_not_allowed")
            decision = _decision(request, latest_observation, action, "operator", simulation_time_s, wall_time_s,
                                 human_identity_authenticated, self._signing_key)
            self._retain_decision(request, decision, database)
            return _copy(decision)

    def expire(self, *, context, request_sha256, simulation_time_s, wall_time_s, run_active, latest_observation=None):
        context = _snapshot(context, "invalid_resolution_context")
        if latest_observation is not None:
            latest_observation = _snapshot(latest_observation, "invalid_resolution_observation")
        with self._transaction() as database:
            request = self._bound(context, request_sha256, database)
            _require(type(run_active) is bool and run_active, "resolution_run_not_active")
            _clock(request, simulation_time_s, wall_time_s)
            _require(_expired(request, simulation_time_s, wall_time_s), "resolution_deadline_not_expired")
            observation = request["initial_observation"] if latest_observation is None else latest_observation
            _observation(observation)
            _evidence_time(request, observation, simulation_time_s, wall_time_s, fresh=False)
            decision = _decision(request, observation, None, "timeout_fallback", simulation_time_s, wall_time_s,
                                 False, self._signing_key)
            self._retain_decision(request, decision, database)
            return _copy(decision)

    def expire_or_get(self, *, context, request_sha256, simulation_time_s, wall_time_s,
                      run_active, latest_observation=None):
        """Atomically expire a bound request or return its existing winner.

        Deadline/liveness/context checks remain identical to ``expire``. A
        concurrent operator decision is retained unchanged; this does not
        reapprove it, refresh its evidence or extend any deadline.
        """
        context = _snapshot(context, "invalid_resolution_context")
        if latest_observation is not None:
            latest_observation = _snapshot(latest_observation, "invalid_resolution_observation")
        with self._transaction() as database:
            _context(context)
            request, retained = self._load(context["request_id"], database)
            _require(request is not None, "no_pending_resolution_request")
            _require(_hash(request_sha256) and context == request["context"]
                and request_sha256 == request["sha256"], "resolution_context_binding_mismatch")
            _require(type(run_active) is bool and run_active, "resolution_run_not_active")
            _clock(request, simulation_time_s, wall_time_s)
            _require(_expired(request, simulation_time_s, wall_time_s), "resolution_deadline_not_expired")
            observation = request["initial_observation"] if latest_observation is None else latest_observation
            _observation(observation)
            _evidence_time(request, observation, simulation_time_s, wall_time_s, fresh=False)
            if retained is not None:
                return _copy(retained)
            decision = _decision(request, observation, None, "timeout_fallback", simulation_time_s, wall_time_s,
                                 False, self._signing_key)
            self._retain_decision(request, decision, database)
            return _copy(decision)

    def records(self, context):
        context = _snapshot(context, "invalid_resolution_context")
        with self._transaction() as database:
            _context(context)
            request, decision = self._load(context["request_id"], database)
            _require(request is not None and request["context"] == context, "resolution_context_binding_mismatch")
            return {"request": _copy(request), "decision": _copy(decision)}


def verify_operator_resolution(request, decision, *, signing_key, expected_context, expected_latest_observation,
                               expected_run_active):
    """Check supplied transaction evidence, not ledger completeness or execution."""
    result = {"passed": False, "issues": [], "ledger_completeness_verified": False,
              "authenticated_human_independently_verified": False, "executor_outcome_verified": False,
              "physical_execution": False, "model_value_established": False}
    try:
        request = _snapshot(request, "invalid_resolution_request")
        decision = _snapshot(decision, "invalid_resolution_decision")
        expected_context = _snapshot(expected_context, "invalid_resolution_context")
        expected_latest_observation = _snapshot(expected_latest_observation, "invalid_resolution_observation")
        _key(signing_key)
        _context(expected_context)
        _request(request, signing_key)
        _observation(expected_latest_observation)
        _require(type(decision) is dict and set(decision) == _DECISION_FIELDS, "invalid_resolution_decision")
        _context(decision["context"])
        _observation(decision["latest_observation"])
        _require(all(_hash(decision[name]) for name in ("request_sha256", "request_observed_evidence_sha256",
                                                       "observed_evidence_sha256")), "invalid_resolution_hash")
        _require(request["context"] == expected_context and decision["context"] == expected_context
                 and decision["request_sha256"] == request["sha256"]
                 and decision["request_observed_evidence_sha256"] == request["observed_evidence_sha256"]
                 and decision["observed_evidence_sha256"] == expected_latest_observation["evidence_sha256"]
                 and decision["latest_observation"] == expected_latest_observation, "resolution_record_binding_mismatch")
        _require(type(expected_run_active) is bool and expected_run_active is True
                 and decision["run_active"] is expected_run_active and type(decision["human_identity_authenticated"]) is bool,
                 "invalid_resolution_run_or_identity")
        sim, wall = decision["simulation_time_s"], decision["wall_time_s"]
        _clock(request, sim, wall)
        source = decision["source"]
        _require(type(source) is str, "invalid_resolution_source")
        if source == "operator":
            _require(not _expired(request, sim, wall), "resolution_deadline_expired")
            _require(type(decision["requested_action"]) is str and decision["requested_action"] in request["allowed_actions"],
                     "resolution_action_not_allowed")
            _evidence_time(request, expected_latest_observation, sim, wall, fresh=True)
        else:
            _require(source == "timeout_fallback" and decision["requested_action"] is None
                     and decision["human_identity_authenticated"] is False and _expired(request, sim, wall),
                     "invalid_timeout_resolution")
            _evidence_time(request, expected_latest_observation, sim, wall, fresh=False)
        # Independent Rules reconstruction; never call the decision producer.
        ready, action = expected_latest_observation["tower_ready"], decision["requested_action"]
        if source == "timeout_fallback":
            expected_action, reason = "divert", "original_deadline_expired"
        elif ready is False:
            expected_action, reason = "divert", "tower_unavailable_forces_divert"
        elif action == "continue_capture":
            _require(ready is True, "tower_readiness_unknown")
            expected_action, reason = "continue_capture", "fresh_tower_ready"
        else:
            expected_action, reason = "divert", "operator_selected_divert"
        _require(all(type(decision[name]) is str for name in ("schema", "scope", "effective_action", "reason"))
                 and decision["schema"] == DECISION_SCHEMA and decision["scope"] == SCOPE
                 and decision["effective_action"] == expected_action and decision["reason"] == reason
                 and decision["operator_response_received"] is (source == "operator")
                 and decision["request_slot_consumed"] is True and decision["requires_executor_rules_check"] is True,
                 "resolution_rules_mismatch")
        _require(all(decision[name] is False for name in (
            "execution_authority_created", "executor_command_issued", "observed_effect_verified", "mission_completed",
            "physical_execution", "numeric_flight_authority", "limits_waived", "model_value_established")),
            "resolution_claim_boundary")
        _require(_hash(decision["signature"]) and hmac.compare_digest(decision["signature"], _signature(decision, signing_key)),
                 "resolution_decision_signature_mismatch")
        result["passed"] = True
    except (OperatorResolutionError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        result["issues"].append("Invalid bound operator-resolution transaction")
    return result

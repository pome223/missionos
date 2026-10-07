"""Host-owned bounded tower proposal/human mailbox for a live simulator child.

Provider access is confined to an explicitly supplied host judge. A per-run
local integrity key and SQLite ledger may be shared with the child; provider
keys are never serialized. Replies are proposals or signed operator records,
not actuator commands, diversion trajectories or observed mission outcomes.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from threading import Lock
import time
from uuid import uuid4

from .starship_operator_resolution import OperatorResolutionLedger, SCOPE
from .starship_tower_supervision import (
    ACTIONS, OBSERVATION_INTERVAL_S, REQUEST_SCHEMA, TowerRoutingAdapter, _context,
    _observation, _scope,
)

FRAME_SCHEMA = "missionos.starship_tower_live_observation_frame.v1"
STATUS_SCHEMA = "missionos.starship_tower_broker_status.v1"
MAX_FRAME_BYTES = 64*1024
_FRAME_FIELDS = {"schema", "context", "source_sha256", "observation", "simulation_time_s", "wall_time_s", "run_active"}
_NAMES = {"latest-observation.json", "observation-request.json", "human-request.json", "human-response.json",
          "broker-status.json", *(f"routing-{kind}-{index}.json" for kind in ("request", "response") for index in (1, 2))}


class TowerBrokerError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise TowerBrokerError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _copy(value):
    try:
        return json.loads(_canonical(value))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise TowerBrokerError("invalid_tower_broker_json") from None


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _clock(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_tower_frame_key")
        result[key] = value
    return result


def _nonfinite(_):
    raise TowerBrokerError("nonfinite_tower_frame")


def _directory(mailbox):
    path = Path(mailbox)
    _require(not path.is_symlink(), "invalid_tower_mailbox")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = path.stat()
    _require(path.is_dir() and metadata.st_uid == os.getuid() and metadata.st_mode & 0o077 == 0,
             "tower_mailbox_must_be_private")
    return path


def write_tower_frame(mailbox, name, value, *, replace=False):
    """Credential-free private writer; only the latest/status files are mutable."""
    _require(name in _NAMES and type(replace) is bool and (not replace or name in
             {"latest-observation.json", "broker-status.json"}), "invalid_tower_frame_name")
    root = _directory(mailbox)
    raw = _canonical(_copy(value))
    _require(len(raw) <= MAX_FRAME_BYTES, "tower_frame_too_large")
    target = root/name
    _require(not target.is_symlink(), "symlink_tower_frame")
    temporary = root/("."+name+"."+uuid4().hex+".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        return _digest(value)
    finally:
        temporary.unlink(missing_ok=True)


def _read(root, name):
    _require(name in _NAMES, "invalid_tower_frame_name")
    path = root/name
    if not path.exists() and not path.is_symlink():
        return None
    _require(not path.is_symlink(), "symlink_tower_frame")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as file:
        metadata = os.fstat(file.fileno())
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                 and 0 < metadata.st_size <= MAX_FRAME_BYTES, "invalid_tower_frame_file")
        raw = file.read(MAX_FRAME_BYTES+1)
    _require(len(raw) <= MAX_FRAME_BYTES, "tower_frame_too_large")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite)
    _require(type(value) is dict and raw == _canonical(value), "noncanonical_tower_frame")
    return value


class TowerBroker:
    """One active child, at most two route calls and one signed operator choice."""

    def __init__(self, mailbox, run_id, scope, process, ledger, judge, source_is_current, *, mode="fixture", clock=time.monotonic):
        scope = _copy(scope)
        _scope(scope)
        _require(run_id == scope["context"]["run_id"] and callable(getattr(process, "poll", None))
            and type(ledger) is OperatorResolutionLedger and ledger._store_path is not None
            and callable(source_is_current) and callable(clock), "invalid_tower_broker_context")
        self._root = _directory(mailbox)
        _require(not any((self._root/name).exists() or (self._root/name).is_symlink() for name in
            ("routing-response-1.json", "routing-response-2.json", "human-response.json", "broker-status.json")),
            "tower_broker_exchange_already_started")
        self._scope, self._process, self._ledger = scope, process, ledger
        self._source_is_current, self._clock = source_is_current, clock
        self._router = TowerRoutingAdapter(judge, mode=mode)
        self._lock = Lock()
        self._routes, self._requests, self._inflight = {}, {}, None
        self._human = None
        self._last_frame = None
        self._seen = {}
        self._status = {"schema": STATUS_SCHEMA, "context": scope["context"], "status": "waiting_for_child",
            "routing_calls_attempted": 0, "routing_replies_published": 0, "human_request_adopted": False,
            "human_decision_published": False, "provider_retries": 0, "followup_calls": 0,
            "provider_keys_serialized": False, "executor_command_issued": False, "physical_effect_verified": False,
            "mission_completed": False, "model_value_established": False, "physical_execution": False}
        # Exclusive publication also closes a concurrent-constructor race;
        # a restart must not silently reserve another provider budget.
        write_tower_frame(self._root, "broker-status.json", self._status)

    def _alive(self):
        try:
            return self._process.poll() is None and self._source_is_current() is True
        except Exception:
            return False

    def _latest(self):
        frame = _read(self._root, "latest-observation.json")
        _require(frame is not None and set(frame) == _FRAME_FIELDS and frame["schema"] == FRAME_SCHEMA,
                 "missing_or_invalid_tower_live_frame")
        _observation(frame["observation"])
        observation = frame["observation"]
        _require(frame["context"] == self._scope["context"] == observation["context"]
            and frame["source_sha256"] == self._scope["source_sha256"] and type(frame["run_active"]) is bool
            and _clock(frame["simulation_time_s"]) and _clock(frame["wall_time_s"])
            and frame["simulation_time_s"] >= observation["simulation_time_s"]
            and frame["wall_time_s"] >= observation["wall_time_s"], "tower_live_frame_binding_mismatch")
        if self._last_frame is not None:
            previous = self._last_frame
            _require(frame["simulation_time_s"] >= previous["simulation_time_s"] and frame["wall_time_s"] >= previous["wall_time_s"]
                and observation["simulation_time_s"] >= previous["observation"]["simulation_time_s"]
                and observation["wall_time_s"] >= previous["observation"]["wall_time_s"], "tower_live_frame_clock_regressed")
            if observation["observation_id"] == previous["observation"]["observation_id"]:
                _require(observation == previous["observation"], "tower_live_observation_reused_changed")
        known = self._seen.get(observation["observation_id"])
        _require(known is None or known == observation, "tower_live_observation_reused_changed")
        _require(known is not None or len(self._seen) < 2048, "tower_broker_observation_budget")
        self._seen[observation["observation_id"]] = observation
        self._last_frame = frame
        return frame

    def _expired(self, frame, now):
        return now >= self._scope["original_wall_deadline_s"] or frame["simulation_time_s"] >= self._scope["original_simulation_deadline_s"]

    def _fresh(self, frame, now):
        observation, age = frame["observation"], self._scope["maximum_observation_age_s"]
        return (frame["run_active"] is True and _clock(now) and 0 <= now-frame["wall_time_s"] <= age
                and 0 <= now-observation["wall_time_s"] <= age
                and 0 <= frame["simulation_time_s"]-observation["simulation_time_s"] <= age)

    def _adopt_human(self):
        request = _read(self._root, "human-request.json")
        if request is None:
            return
        records = self._ledger.records(self._scope["context"])
        _require(request == records["request"] and request["scope"] == SCOPE
            and request["context"] == self._scope["context"] and request["allowed_actions"] == list(ACTIONS)
            and request["original_simulation_deadline_s"] == self._scope["original_simulation_deadline_s"]
            and request["original_wall_deadline_s"] == self._scope["original_wall_deadline_s"]
            and request["maximum_observation_age_s"] == self._scope["maximum_observation_age_s"]
            and self._scope["human_resolution_allowed"] is True, "unbound_tower_human_request")
        _require(self._human is None or request == self._human, "tower_human_request_changed")
        self._human = request
        self._status["human_request_adopted"] = True
        if records["decision"] is None and not self._status["human_decision_published"]:
            self._status["status"] = "awaiting_human"

    def _publish_human(self, decision):
        existing = _read(self._root, "human-response.json")
        _require(existing is None or existing == decision, "tower_human_response_conflict")
        if existing is None:
            write_tower_frame(self._root, "human-response.json", decision)
        self._status.update(human_decision_published=True, status="human_decision_published")

    def _save_status(self):
        write_tower_frame(self._root, "broker-status.json", self._status, replace=True)

    def pending(self):
        """Fresh display metadata; it never invokes a classifier or grants a choice."""
        with self._lock:
            self._adopt_human()
            if _read(self._root, "latest-observation.json") is None:
                return {"schema": "missionos.starship_tower_operator_pending.v1", "context": _copy(self._scope["context"]),
                    "pending": False, "request": _copy(self._human), "latest_observation": None, "decision": None,
                    "run_active": self._alive(), "fresh": False, "expired": self._clock() >= self._scope["original_wall_deadline_s"],
                    "original_simulation_deadline_s": self._scope["original_simulation_deadline_s"],
                    "original_wall_deadline_s": self._scope["original_wall_deadline_s"],
                    "human_identity_authenticated": False, "executor_command_issued": False}
            frame, now = self._latest(), self._clock()
            active = self._alive() and frame["run_active"] is True
            fresh, expired = self._fresh(frame, now), self._expired(frame, now)
            records = self._ledger.records(self._scope["context"]) if self._human is not None else None
            decision = records["decision"] if records is not None else None
            return {"schema": "missionos.starship_tower_operator_pending.v1", "context": _copy(self._scope["context"]),
                "pending": self._human is not None and decision is None and active and fresh and not expired,
                "request": _copy(self._human), "latest_observation": _copy(frame["observation"]),
                "decision": _copy(decision), "run_active": active, "fresh": fresh, "expired": expired,
                "original_simulation_deadline_s": self._scope["original_simulation_deadline_s"],
                "original_wall_deadline_s": self._scope["original_wall_deadline_s"],
                "human_identity_authenticated": False, "executor_command_issued": False}

    def resolve(self, *, context, request_sha256, observed_evidence_sha256, action):
        """Consume one current signed request, always with caller identity unproven."""
        context = _copy(context)
        _context(context)
        _require(_hash(request_sha256) and _hash(observed_evidence_sha256)
                 and type(action) is str and action in ACTIONS, "invalid_tower_operator_choice")
        with self._lock:
            _require(context == self._scope["context"], "tower_operator_cross_context")
            self._adopt_human()
            frame, now = self._latest(), self._clock()
            _require(self._human is not None and request_sha256 == self._human["sha256"], "no_bound_tower_human_pending")
            _require(self._alive() and frame["run_active"] is True, "tower_operator_run_not_active")
            _require(not self._expired(frame, now), "tower_operator_original_deadline_expired")
            _require(self._fresh(frame, now), "tower_operator_observation_stale")
            observation = frame["observation"]
            _require(observed_evidence_sha256 == observation["evidence_sha256"], "tower_operator_latest_evidence_changed")
            decision = self._ledger.resolve(context=context, request_sha256=request_sha256,
                observed_evidence_sha256=observed_evidence_sha256,
                latest_observation={key: observation[key] for key in ("evidence_sha256", "simulation_time_s", "wall_time_s", "tower_ready")},
                action=action, simulation_time_s=frame["simulation_time_s"], wall_time_s=now, run_active=True,
                human_identity_authenticated=False)
            self._publish_human(decision)
            self._save_status()
            return _copy(decision)

    def _route_request(self, index, frame, now):
        request = _read(self._root, f"routing-request-{index}.json")
        if request is None:
            return None
        _require(request.get("schema") == REQUEST_SCHEMA and request.get("round") == index
            and request.get("context") == self._scope["context"] and request.get("source_sha256") == self._scope["source_sha256"]
            and request.get("approval_record_sha256") == self._scope["approval_record_sha256"]
            and request.get("original_simulation_deadline_s") == self._scope["original_simulation_deadline_s"]
            and request.get("original_wall_deadline_s") == self._scope["original_wall_deadline_s"]
            and request.get("maximum_observation_age_s") == self._scope["maximum_observation_age_s"], "tower_routing_scope_mismatch")
        age = self._scope["maximum_observation_age_s"]
        observation = request["observation"]
        _require(0 <= now-observation["wall_time_s"] <= age
            and 0 <= frame["simulation_time_s"]-observation["simulation_time_s"] <= age,
            "tower_routing_request_stale")
        if index == 2:
            operation = _read(self._root, "observation-request.json")
            fields = {"schema", "issued_evidence_sha256", "issued_state_sha256", "issued_simulation_time_s", "issued_wall_time_s",
                      "earliest_simulation_time_s", "original_simulation_deadline_s", "original_wall_deadline_s",
                      "completed_evidence_sha256", "actuator_operation"}
            _require(self._scope["observation_collection_allowed"] is True and 1 in self._routes and operation is not None
                and set(operation) == fields and self._routes[1]["route"] in ("need_observation", "bounded")
                and (self._routes[1]["route"] != "bounded" or self._routes[1]["proposed_action"] == "continue_capture")
                and operation.get("schema") == "missionos.starship_tower_observation_request.v1"
                and operation.get("actuator_operation") is False
                and operation.get("original_simulation_deadline_s") == self._scope["original_simulation_deadline_s"]
                and operation.get("original_wall_deadline_s") == self._scope["original_wall_deadline_s"]
                and _clock(operation.get("issued_simulation_time_s"))
                and operation["issued_simulation_time_s"] >= self._requests[1]["issued_simulation_time_s"]
                and _clock(operation.get("issued_wall_time_s")) and operation["issued_wall_time_s"] <= now
                and _hash(operation["issued_evidence_sha256"]) and _hash(operation["issued_state_sha256"])
                and operation["completed_evidence_sha256"] is None
                and operation.get("earliest_simulation_time_s") == operation["issued_simulation_time_s"]+OBSERVATION_INTERVAL_S
                and observation["simulation_time_s"] >= operation["earliest_simulation_time_s"]
                and observation["simulation_time_s"] > self._requests[1]["observation"]["simulation_time_s"],
                "tower_followup_lacks_later_observation_collection")
        return request

    def step(self):
        """Bounded host work; the child continues integrating in another process."""
        with self._lock:
            reserved = False
            if self._status["status"] in ("routing_request_rejected_without_retry", "broker_protocol_rejected"):
                return _copy(self._status)
            if not self._alive():
                self._status["status"] = "stopped_child_or_source"
                self._save_status()
                return _copy(self._status)
            if _read(self._root, "latest-observation.json") is None:
                self._status["status"] = ("original_deadline_expired_without_frame" if self._clock() >= self._scope["original_wall_deadline_s"]
                                          else "waiting_for_child")
                self._save_status()
                return _copy(self._status)
            frame, now = self._latest(), self._clock()
            self._adopt_human()
            if frame["run_active"] is not True:
                self._status["status"] = "stopped_child_frame"
            elif self._expired(frame, now):
                if self._human is not None:
                    decision = self._ledger.expire_or_get(context=self._scope["context"],
                        request_sha256=self._human["sha256"], simulation_time_s=frame["simulation_time_s"],
                        wall_time_s=now, run_active=True,
                        latest_observation={key: frame["observation"][key] for key in ("evidence_sha256", "simulation_time_s", "wall_time_s", "tower_ready")})
                    self._publish_human(decision)
                self._status["status"] = "original_deadline_expired"
            elif not self._fresh(frame, now):
                self._status["status"] = "waiting_fresh_child_frame"
            elif self._human is not None:
                decision = self._ledger.records(self._scope["context"])["decision"]
                if decision is not None:
                    self._publish_human(decision)
            elif self._inflight is None:
                index = len(self._routes)+1
                request = self._route_request(index, frame, now) if index <= 2 else None
                if request is not None:
                    reserved = True
                    self._inflight = index
                    self._requests[index] = request
                    self._status["routing_calls_attempted"] += 1
                    self._status["followup_calls"] = int(index == 2)
                    self._status["status"] = "classifying_proposal_only"
                    self._save_status()
                else:
                    self._save_status()
                    return _copy(self._status)
            else:
                self._save_status()
                return _copy(self._status)
            if not reserved:
                self._save_status()
                return _copy(self._status)
            index, request = self._inflight, self._requests[self._inflight]
        # No lock or child wait is held across provider latency. One adapter
        # slot is spent even if the result arrives after lifecycle invalidation.
        try:
            response = self._router.assess(request, run_active=True, source_current=True)
        except Exception:
            with self._lock:
                self._inflight = None
                self._routes[index] = None
                self._status["status"] = "routing_request_rejected_without_retry"
                self._save_status()
                return _copy(self._status)
        with self._lock:
            self._inflight = None
            self._routes[index] = response
            frame, now = self._latest(), self._clock()
            if self._alive() and self._fresh(frame, now) and not self._expired(frame, now):
                _require(_read(self._root, f"routing-request-{index}.json") == request, "tower_request_changed_during_provider_call")
                write_tower_frame(self._root, f"routing-response-{index}.json", response)
                self._status["routing_replies_published"] += 1
                self._status["status"] = "routing_proposal_published"
            else:
                self._status["status"] = "late_routing_proposal_not_published"
            self._save_status()
            return _copy(self._status)

    def serve(self, *, poll_interval_s=.05):
        _require(type(poll_interval_s) in (int, float) and .01 <= poll_interval_s <= 1., "invalid_tower_broker_poll_interval")
        while True:
            try:
                status = self.step()
            except Exception:
                with self._lock:
                    self._status["status"] = "broker_protocol_rejected"
                    self._save_status()
                    return _copy(self._status)
            if status["status"] in ("stopped_child_or_source", "stopped_child_frame", "original_deadline_expired",
                                     "original_deadline_expired_without_frame", "human_decision_published",
                                     "late_routing_proposal_not_published", "routing_request_rejected_without_retry"):
                return status
            time.sleep(poll_interval_s)

    def status(self):
        with self._lock:
            return _copy(self._status)


def serve_tower_request(mailbox, run_id, scope, process, ledger, judge, source_is_current, *, mode="fixture",
                        clock=time.monotonic, poll_interval_s=.05):
    broker = TowerBroker(mailbox, run_id, scope, process, ledger, judge, source_is_current, mode=mode, clock=clock)
    return broker.serve(poll_interval_s=poll_interval_s)

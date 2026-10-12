"""Nonblocking host approval to worker reservation changes, not burn dispatch.

HMAC keys and operator bearer tokens stay on the host. A worker only reads a
host-owned inherited pipe. Its checks bind a host result to the outstanding
request; they do not claim to verify the host HMAC without the key.
"""
from copy import deepcopy
import fcntl
import json
import math
import os
import re
import stat
import time

from .starship_inflight_approval import InflightApprovalError, digest

MAX_FRAME_BYTES = 16384
# Source-bound scheduling assumptions, not authority extensions or a guarantee
# of human response. The original simulation/wall deadlines remain mandatory.
APPROVAL_RESPONSE_RESERVE_S = 20.0
APPROVAL_COAST_GUARD_S = 4.002
_HASH = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _require(value, reason):
    if not value:
        raise InflightApprovalError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def amendment_window(bounds, *, simulation_time_s, wall_time_s, model_response_reserve_s,
                     status, scope_revision, attempted=False, telemetry_available=True):
    """Expose time needed for a new request without granting that request.

    The model reserve is the maximum approved decision window, so a shorter
    Jev decision cannot accidentally spend the following approval interval.
    """
    simulation_remaining = max(0.0, bounds["simulation_deadline_s"]-simulation_time_s)
    wall_remaining = max(0.0, bounds["wall_deadline_s"]-wall_time_s)
    lead = model_response_reserve_s + APPROVAL_RESPONSE_RESERVE_S + APPROVAL_COAST_GUARD_S
    if scope_revision:
        reason = "scope_already_amended"
    elif status != "idle" or attempted:
        reason = "request_already_attempted"
    elif not telemetry_available:
        reason = "trusted_telemetry_unavailable"
    elif min(simulation_remaining, wall_remaining) <= 0:
        reason = "original_deadline_expired"
    elif min(simulation_remaining, wall_remaining) < lead:
        reason = "insufficient_model_and_operator_time"
    else:
        reason = "request_window_open"
    return {"schema": "missionos.inflight_request_window.v1",
        "simulation_deadline_s": bounds["simulation_deadline_s"],
        "remaining_simulation_s": simulation_remaining, "remaining_wall_s": wall_remaining,
        "model_response_reserve_s": model_response_reserve_s,
        "operator_response_reserve_s": APPROVAL_RESPONSE_RESERVE_S,
        "coast_guard_s": APPROVAL_COAST_GUARD_S,
        "latest_decision_start_s": bounds["simulation_deadline_s"]-lead,
        "request_attempted": attempted, "trusted_telemetry_available": telemetry_available,
        "request_available": reason == "request_window_open", "reason": reason}


def _context(value):
    _require(type(value) is dict and set(value) == {"run_id", "mission_id", "vehicle_id"}
             and all(type(v) is str and _ID.fullmatch(v) for v in value.values()), "invalid_runtime_context")


def _snapshot(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _record_event(owner, sink, name, **fields):
    row = _snapshot({"event": name, **fields})
    owner.events.append(row)
    sink(name, **deepcopy(fields))


def _bounds(value):
    required = {"simulation_deadline_s", "wall_deadline_s", "maximum_observation_age_s"}
    _require(type(value) is dict and set(value) in (required, required | {"reservation_cutoff_s"})
             and all(_number(v) for v in value.values()) and 0 < value["maximum_observation_age_s"] <= 30,
             "invalid_runtime_deadline_bounds")
    if "reservation_cutoff_s" in value:
        _require(value["simulation_deadline_s"] <= value["reservation_cutoff_s"],
                 "approval_deadline_exceeds_reserved_return_cutoff")


def _decode(raw):
    def pairs(entries):
        value = dict(entries)
        _require(len(value) == len(entries), "duplicate_pipe_key")
        return value
    def constant(_value):
        raise InflightApprovalError("invalid_pipe_number")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    _require(type(value) is dict, "invalid_pipe_frame")
    return value


class ApprovalPipeReader:
    """Own exactly the read end; poll never waits for a frame."""

    def __init__(self, fd):
        _require(stat.S_ISFIFO(os.fstat(fd).st_mode)
                 and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY, "read_only_pipe_required")
        self.fd, self._buffer, self.closed = fd, b"", False
        os.set_blocking(fd, False)

    def poll(self):
        if b"\n" not in self._buffer and not self.closed:
            try:
                data = os.read(self.fd, MAX_FRAME_BYTES+1-len(self._buffer))
                if not data:
                    self.closed = True
                self._buffer += data
            except BlockingIOError:
                pass
        if b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            _require(len(raw) <= MAX_FRAME_BYTES, "pipe_frame_too_large")
            return _decode(raw)
        _require(len(self._buffer) <= MAX_FRAME_BYTES, "pipe_frame_too_large")
        _require(not self.closed or not self._buffer, "truncated_pipe_frame")
        return None

    def close(self):
        os.close(self.fd)
        self.closed = True


class ApprovalPipeWriter:
    """Bounded nonblocking writes; the caller polls flush while physics runs."""

    def __init__(self, fd):
        _require(stat.S_ISFIFO(os.fstat(fd).st_mode)
                 and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY, "write_only_pipe_required")
        self.fd, self._buffer = fd, b""
        os.set_blocking(fd, False)

    def send(self, frame):
        _require(not self._buffer, "pipe_writer_busy")
        raw = json.dumps(frame, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        _require(len(raw) <= MAX_FRAME_BYTES, "pipe_frame_too_large")
        self._buffer = raw+b"\n"
        self.flush()

    def flush(self):
        if self._buffer:
            try:
                self._buffer = self._buffer[os.write(self.fd, self._buffer):]
            except BlockingIOError:
                pass
        return not self._buffer

    def close(self):
        os.close(self.fd)


def approval_pipe():
    """Create in the parent, then inherit only each process's owned endpoints."""
    read_fd, write_fd = os.pipe()
    return ApprovalPipeReader(read_fd), ApprovalPipeWriter(write_fd)


def _state(state, now, maximum_age):
    _require(type(state) is dict and state.get("run_active") is True and type(state.get("revision")) is int
             and state["revision"] >= 0 and _number(state.get("simulation_time_s")) and _number(now)
             and type(state.get("scope")) is dict and _hash(state.get("reservation_sha256")), "invalid_runtime_state")
    obs = state.get("observation")
    _require(type(obs) is dict and set(obs) == {"state_sha256", "simulation_time_s", "wall_time_s"}
             and _hash(obs["state_sha256"]) and _number(obs["simulation_time_s"]) and _number(obs["wall_time_s"])
             and 0 <= state["simulation_time_s"]-obs["simulation_time_s"] <= maximum_age
             and 0 <= now-obs["wall_time_s"] <= maximum_age, "runtime_observation_not_fresh")


class HostApprovalBridge:
    """Host-only ledger consumer; never give this object/key/token to a model."""

    def __init__(self, *, ledger, context, snapshot, request_reader, grant_writer, bounds,
                 event=lambda *_args, **_kwargs: None, clock=time.monotonic):
        _context(context)
        _bounds(bounds)
        _require(isinstance(request_reader, ApprovalPipeReader) and isinstance(grant_writer, ApprovalPipeWriter),
                 "inherited_approval_pipes_required")
        self.ledger, self.context, self._snapshot = ledger, deepcopy(context), snapshot
        self.reader, self.writer, self.bounds, self.clock = request_reader, grant_writer, deepcopy(bounds), clock
        self.events = []
        self.event = lambda name, **fields: _record_event(self, event, name, **fields)
        self.frame, self.request, self.finished = None, None, False
        self.result = None

    def record(self):
        return _snapshot({"schema": "missionos.inflight_host_runtime_record.v1", "context": self.context,
            "bounds": self.bounds, "request_frame": self.frame, "request": self.request,
            "result": self.result, "events": self.events, "executor_command_issued": False})

    def snapshot(self):
        value = self._snapshot()
        return {key: deepcopy(value[key]) for key in ("run_active", "simulation_time_s", "revision", "scope", "observation")} | {
            "pending_request_id": self.request["request_id"] if self.request else None}

    def _begin(self, frame, state, now):
        _require(self.frame is None, "inflight_request_budget_exhausted")
        _require(type(frame) is dict and set(frame) == {"schema", "context", "request_id", "action", "candidate_sha256",
            "revision", "scope_sha256", "observation", "simulation_time_s", "simulation_deadline_s", "wall_deadline_s", "fallback"}
            and frame["schema"] == "missionos.inflight_scope_request.v1" and frame["context"] == self.context,
            "invalid_worker_scope_request")
        _state(state, now, self.bounds["maximum_observation_age_s"])
        _require(type(frame["revision"]) is int and frame["revision"] == state["revision"]
            and frame["scope_sha256"] == digest(state["scope"]) and _hash(frame["candidate_sha256"])
            and frame["action"] == "select_next_orbit" and frame["fallback"] in state["scope"]["commands"]
            and _number(frame["simulation_time_s"]) and frame["simulation_time_s"] <= state["simulation_time_s"]
            and _number(frame["simulation_deadline_s"]) and state["simulation_time_s"] < frame["simulation_deadline_s"] <= self.bounds["simulation_deadline_s"]
            and _number(frame["wall_deadline_s"]) and now < frame["wall_deadline_s"] <= self.bounds["wall_deadline_s"],
            "scope_request_outside_original_bounds")
        # The worker may have advanced while its request was in transit. Only
        # host-observed history may authenticate that earlier observation.
        history = state.get("known_observations", [])
        _require(type(history) is list and len(history) <= 64, "invalid_host_observation_history")
        trusted = [state["observation"], *history]
        for observation in trusted:
            _require(type(observation) is dict
                and set(observation) == {"state_sha256", "simulation_time_s", "wall_time_s"}
                and _hash(observation["state_sha256"])
                and _number(observation["simulation_time_s"]) and _number(observation["wall_time_s"])
                and observation["simulation_time_s"] <= state["simulation_time_s"]
                and observation["wall_time_s"] <= now, "invalid_host_observation_history")
        _require(type(frame["observation"]) is dict and digest(frame["observation"]) in {digest(obs) for obs in trusted},
                 "worker_observation_not_host_observed")
        self.request = self.ledger.create(request_id=frame["request_id"], mission_id=self.context["mission_id"],
            vehicle_id=self.context["vehicle_id"], revision=state["revision"], scope=state["scope"],
            delta={"add_commands": [frame["action"]], "resource_limits": {}}, observation=frame["observation"],
            simulation_time_s=state["simulation_time_s"], simulation_deadline_s=frame["simulation_deadline_s"],
            wall_deadline_s=frame["wall_deadline_s"], fallback=frame["fallback"],
            maximum_observation_age_s=self.bounds["maximum_observation_age_s"], candidate_sha256=frame["candidate_sha256"])
        self.frame = deepcopy(frame)
        self.event("m3_operator_request_registered", time_s=state["simulation_time_s"], request_sha256=self.request["sha256"])

    def _reject_input(self, state, reason):
        result = {"schema": "missionos.inflight_host_rejection.v1", "context": self.context,
            "reason": reason, "executor_command_issued": False}
        delivered = False
        try:
            self.writer.send(result)
            delivered = True
        except (InflightApprovalError, OSError):
            pass
        self.result, self.finished = deepcopy(result), True
        simulation = state.get("simulation_time_s") if type(state) is dict else None
        self.event("m3_host_approval_input_rejected", time_s=simulation if _number(simulation) else None,
                   reason=reason, delivery_queued=delivered, executor_command_issued=False)
        return deepcopy(result)

    def tick(self):
        try:
            self.writer.flush()
        except OSError:
            if not self.finished:
                return self._reject_input(None, "host_grant_channel_closed")
            return None
        if self.finished:
            return None
        state, now = self._snapshot(), self.clock()
        try:
            incoming = self.reader.poll()
            if incoming is not None:
                self._begin(incoming, state, now)
        except (InflightApprovalError, KeyError, TypeError, ValueError, OSError):
            return self._reject_input(state, "invalid_worker_scope_request")
        if self.request is None:
            if self.reader.closed:
                return self._reject_input(state, "worker_request_channel_closed")
            return None
        request = self.request
        records = self.ledger.status(request["request_id"])
        if (records["receipt"] is None and state["simulation_time_s"] < request["simulation_deadline_s"]
                and now < request["wall_deadline_s"]):
            return None
        try:
            consumed = self.ledger.consume(request_id=request["request_id"], mission_id=self.context["mission_id"],
                vehicle_id=self.context["vehicle_id"], revision=state["revision"], scope=state["scope"],
                observation=state["observation"], simulation_time_s=state["simulation_time_s"], run_active=state["run_active"])
            reason = consumed["reason"]
        except InflightApprovalError as exc:
            consumed, reason = None, str(exc)
        result = {"schema": "missionos.inflight_host_result.v1", "context": self.context,
            "request_id": request["request_id"], "request_frame_sha256": digest(self.frame),
            "request": request, "consumption": consumed, "reason": reason}
        self.writer.send(result)
        self.result = deepcopy(result)
        self.finished = True
        self.event("m3_host_approval_result_sent", time_s=state["simulation_time_s"], request_id=request["request_id"], reason=reason)
        return deepcopy(result)


class WorkerApprovalActor:
    """One scope-amendment attempt; tick never integrates or sleeps.

    ``command(action, state, consumption)`` changes only a plan reservation and
    returns applied/revision/reservation_sha256. Burn dispatch remains in the
    existing executor with its final fresh physical check.
    """

    def __init__(self, *, context, scope, revision, request_writer, grant_reader, bounds, admission, command,
                 event=lambda *_args, **_kwargs: None, clock=time.monotonic):
        _context(context)
        _bounds(bounds)
        _require(type(revision) is int and revision >= 0, "invalid_runtime_revision")
        _require(isinstance(request_writer, ApprovalPipeWriter) and isinstance(grant_reader, ApprovalPipeReader),
                 "inherited_approval_pipes_required")
        self.context, self.scope, self.revision = deepcopy(context), deepcopy(scope), revision
        self.writer, self.reader, self.bounds = request_writer, grant_reader, deepcopy(bounds)
        self.admission, self.command, self.clock = admission, command, clock
        self.events = []
        self.event = lambda name, **fields: _record_event(self, event, name, **fields)
        self.initial_scope, self.initial_revision, self.host_result = deepcopy(scope), revision, None
        self.frame, self.applied, self.status, self.last_simulation_time_s = None, None, "idle", None

    def record(self):
        return _snapshot({"schema": "missionos.inflight_worker_runtime_record.v1", "context": self.context,
            "bounds": self.bounds, "initial_scope": self.initial_scope, "initial_revision": self.initial_revision,
            "scope": self.scope, "revision": self.revision, "status": self.status, "request_frame": self.frame,
            "host_result": self.host_result, "applied_reservation": self.applied, "events": self.events,
            "host_signature_verified_by_worker": False, "burn_dispatch_issued": False, "physical_return_verified": False})

    def request_change(self, *, request_id, candidate_sha256, state, fallback="keep_reserved_return"):
        now = self.clock()
        _require(self.status == "idle", "inflight_request_budget_exhausted")
        _state(state, now, self.bounds["maximum_observation_age_s"])
        _require(state["revision"] == self.revision and digest(state["scope"]) == digest(self.scope)
                 and "select_next_orbit" not in self.scope["commands"] and fallback in self.scope["commands"]
                 and type(request_id) is str and _ID.fullmatch(request_id) and _hash(candidate_sha256)
                 and state["simulation_time_s"] < self.bounds["simulation_deadline_s"]
                 and now < self.bounds["wall_deadline_s"], "inflight_change_not_admissible")
        self.frame = {"schema": "missionos.inflight_scope_request.v1", "context": self.context, "request_id": request_id,
            "action": "select_next_orbit", "candidate_sha256": candidate_sha256, "revision": self.revision,
            "scope_sha256": digest(self.scope), "observation": state["observation"], "simulation_time_s": state["simulation_time_s"],
            "simulation_deadline_s": self.bounds["simulation_deadline_s"], "wall_deadline_s": self.bounds["wall_deadline_s"],
            "fallback": fallback}
        self.frame = _snapshot(self.frame)
        self.writer.send(self.frame)
        self.status, self.last_simulation_time_s = "awaiting_operator", state["simulation_time_s"]
        self.event("m3_scope_change_requested", time_s=state["simulation_time_s"], request_id=request_id,
                   candidate_sha256=candidate_sha256, request_frame_sha256=digest(self.frame))
        return deepcopy(self.frame)

    def _fallback(self, state, reason):
        # Never calculate another candidate or change the reservation on timeout.
        try:
            _state(state, self.clock(), self.bounds["maximum_observation_age_s"])
            _require(self.frame["fallback"] in state["scope"]["commands"], "fallback_not_currently_authorized")
            check = self.admission(self.frame["fallback"], deepcopy(state))
            _require(type(check) is dict and type(check.get("accepted")) is bool, "invalid_fallback_check")
        except Exception:
            check = {"accepted": False, "reason": "fresh_fallback_admission_unavailable"}
        self.status = "fallback_preserved" if check.get("accepted") is True else "fallback_not_admitted"
        self.event("m3_scope_update_not_applied", time_s=state["simulation_time_s"], reason=reason,
            request_id=self.frame["request_id"], reservation_changed=False, fallback_check=_snapshot(check),
            burn_dispatch_issued=False, current_reservation_sha256=state["reservation_sha256"])
        return {"status": self.status, "reason": reason, "reservation_changed": False, "burn_dispatch_issued": False}

    def _grant(self, result, state, now):
        if type(result) is dict and result.get("schema") == "missionos.inflight_host_rejection.v1":
            _require(set(result) == {"schema", "context", "reason", "executor_command_issued"}
                and result["context"] == self.context and result["executor_command_issued"] is False
                and result["reason"] in {"invalid_worker_scope_request", "host_grant_channel_closed", "worker_request_channel_closed"},
                "invalid_host_rejection")
            return None
        _require(type(result) is dict and set(result) == {"schema", "context", "request_id", "request_frame_sha256", "request", "consumption", "reason"}
            and result["schema"] == "missionos.inflight_host_result.v1" and result["context"] == self.context
            and result["request_id"] == self.frame["request_id"] and result["request_frame_sha256"] == digest(self.frame), "unbound_host_result")
        request, grant = result["request"], result["consumption"]
        if grant is None:
            return None
        _require(type(request) is dict and type(grant) is dict and type(grant.get("approved")) is bool,
                 "invalid_host_consumption_shape")
        if grant["approved"] is not True:
            return None
        expected_scope = {"commands": self.scope["commands"]+["select_next_orbit"], "resource_limits": self.scope["resource_limits"]}
        _require(grant.get("schema") == "missionos.starship_inflight_approval_consumption.v1"
            and all(type(value) is int for value in (request["current_revision"], grant["previous_revision"], grant["revision"]))
            and _hash(request["signature"]) and _hash(grant["signature"])
            and request["sha256"] == digest({k: v for k, v in request.items() if k not in ("sha256", "signature")})
            and request["request_id"] == self.frame["request_id"] and grant["request_sha256"] == request["sha256"]
            and request.get("candidate_sha256") == grant.get("candidate_sha256") == self.frame["candidate_sha256"]
            and request["mission_id"] == grant["mission_id"] == self.context["mission_id"]
            and request["vehicle_id"] == grant["vehicle_id"] == self.context["vehicle_id"]
            and request["current_revision"] == grant["previous_revision"] == self.revision
            and grant["revision"] == self.revision+1 and digest(grant["scope"]) == digest(expected_scope)
            and grant["scope_sha256"] == digest(expected_scope) and request["current_scope_sha256"] == digest(self.scope)
            and digest(request["current_scope"]) == digest(self.scope) and request["fallback"] == self.frame["fallback"]
            and request["requested_scope_delta"] == {"add_commands": ["select_next_orbit"], "resource_limits": {}}
            and request["simulation_deadline_s"] == self.frame["simulation_deadline_s"]
            and request["wall_deadline_s"] == self.frame["wall_deadline_s"]
            and request["observed_state_sha256"] == self.frame["observation"]["state_sha256"]
            and grant["simulation_only"] is True and grant["physical_execution_authorized"] is False
            and grant["requires_executor_check"] is True and grant["executor_command_issued"] is False
            and grant.get("approval_disposition") == "approved_pending_executor" and grant.get("scope_applied") is False
            and grant.get("revision_semantics") == "authorized_revision_not_applied_revision"
            and self.frame["simulation_time_s"] <= grant["simulation_time_s"] <= state["simulation_time_s"]
            and request["issued_wall_time_s"] <= grant["wall_time_s"] <= now
            and grant["sha256"] == digest({k: v for k, v in grant.items() if k not in ("sha256", "signature")}), "invalid_host_consumption_binding")
        return grant

    def tick(self, state):
        if self.status == "reservation_changed_waiting_observation":
            return self.confirm(state)
        if self.status != "awaiting_operator":
            return None
        now = self.clock()
        command_started = False
        try:
            _state(state, now, self.bounds["maximum_observation_age_s"])
            _require(state["simulation_time_s"] >= self.last_simulation_time_s, "runtime_clock_regressed")
            self.last_simulation_time_s = state["simulation_time_s"]
            _require(state["revision"] == self.revision and digest(state["scope"]) == digest(self.scope), "runtime_context_changed")
            if state["simulation_time_s"] >= self.bounds["simulation_deadline_s"] or now >= self.bounds["wall_deadline_s"]:
                return self._fallback(state, "original_deadline_expired")
            self.writer.flush()
            result = self.reader.poll()
            if result is None:
                return self._fallback(state, "host_channel_closed") if self.reader.closed else None
            self.host_result = deepcopy(result)
            grant = self._grant(result, state, now)
            if grant is None:
                return self._fallback(state, "host_declined_or_invalidated")
            check = self.admission(self.frame["action"], deepcopy(state))
            _require(type(check) is dict and type(check.get("accepted")) is bool, "invalid_candidate_check")
            if check.get("accepted") is not True or check.get("candidate_sha256") != self.frame["candidate_sha256"]:
                return self._fallback(state, "candidate_no_longer_admitted")
            command_started = True
            changed = self.command(self.frame["action"], deepcopy(state), deepcopy(grant))
            _require(type(changed) is dict and changed.get("applied") is True and changed.get("revision") == grant["revision"]
                and _hash(changed.get("reservation_sha256")), "reservation_update_not_confirmed")
            self.scope, self.revision = deepcopy(grant["scope"]), grant["revision"]
            self.applied = {"simulation_time_s": state["simulation_time_s"], "reservation_sha256": changed["reservation_sha256"],
                "revision": self.revision, "consumption_sha256": grant["sha256"]}
            self.status = "reservation_changed_waiting_observation"
            self.event("m3_return_reservation_changed", time_s=state["simulation_time_s"], **{k: v for k, v in self.applied.items() if k != "simulation_time_s"},
                host_result_sha256=digest(result), host_signature_verified_by_worker=False,
                authority_transport="inherited_host_pipe_v1", admission_check=_snapshot(check), burn_dispatch_issued=False)
            return {"status": self.status, "reservation_changed": True, "burn_dispatch_issued": False}
        except Exception as exc:
            # Callback failures do not erase the still-running physical flight.
            # A command may already have changed its reservation: do not retry.
            if command_started:
                self.status = "reservation_update_unconfirmed"
                self.event("m3_reservation_update_unconfirmed", time_s=state["simulation_time_s"],
                    request_id=self.frame["request_id"], reservation_changed=None, retry_authorized=False,
                    burn_dispatch_issued=False)
                return {"status": self.status, "reservation_changed": None, "burn_dispatch_issued": False}
            return self._fallback(state, str(exc) if isinstance(exc, InflightApprovalError) else "approval_channel_or_adapter_failed")

    def confirm(self, state):
        if self.status != "reservation_changed_waiting_observation":
            return None
        try:
            _state(state, self.clock(), self.bounds["maximum_observation_age_s"])
        except InflightApprovalError:
            self.status = "reservation_confirmation_failed"
            self.event("m3_return_reservation_observation_unavailable", time_s=state["simulation_time_s"],
                burn_dispatch_issued=False, physical_return_verified=False)
            return {"status": self.status, "reservation_changed": True, "burn_dispatch_issued": False}
        if state["observation"]["simulation_time_s"] <= self.applied["simulation_time_s"]:
            return None
        matched = (state["revision"] == self.applied["revision"] and digest(state["scope"]) == digest(self.scope)
                   and state["reservation_sha256"] == self.applied["reservation_sha256"])
        self.status = "reservation_confirmed" if matched else "reservation_confirmation_failed"
        self.event("m3_return_reservation_observed", time_s=state["simulation_time_s"], matched=matched,
            observed_state_sha256=state["observation"]["state_sha256"], reservation_sha256=state["reservation_sha256"],
            burn_dispatch_issued=False, physical_return_verified=False)
        return {"status": self.status, "reservation_changed": True, "burn_dispatch_issued": False}

"""Inherited, bounded judgment transport; no approval or plant execution.

The worker only exchanges JSON over its inherited pipes. Only the host creates
an agent. A host request gets one background thread and no retry; DeepSeek is
eligible only after a valid explicit Jev delegation inside the original epoch.
An opt-in V2 policy can require review of a provisional return answer from
uncalibrated Jev metrics; it does not change actions, evidence, or the epoch.
Synthetic transport tests establish no native inference or flight evidence.
"""
from hashlib import sha256
import json
import math
import queue
import threading
import time

from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, MAX_FRAME_BYTES, _context, _hash
from .starship_restored_return_review import validate_policy, policy_digest, review_route

REQUEST_FRAME = "missionos.starship_restored_decision_pipe_request.v1"
RESPONSE_FRAME = "missionos.starship_restored_decision_pipe_response.v1"
EXCHANGE = "missionos.starship_restored_decision_exchange.v1"
REQUEST_FRAME_V2 = "missionos.starship_restored_decision_pipe_request.v2"
RESPONSE_FRAME_V2 = "missionos.starship_restored_decision_pipe_response.v2"
EXCHANGE_V2 = "missionos.starship_restored_decision_exchange.v2"
CLIENT_V2 = "missionos.starship_restored_decision_client.v2"
HOST_V2 = "missionos.starship_restored_decision_host.v2"
WINDOWS = {"deployment": 35., "return": 180.}
BUDGETS = {"typesafe": 35., "deepseek": 150.}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _copy(value):
    return json.loads(_wire(value))


def _digest(value):
    return sha256(_wire(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _enough(provider, remaining):
    return remaining > 0 if provider == "typesafe" else remaining >= BUDGETS[provider]


def _request(value, *, expected_source_sha256=None):
    # This pure validation imports no credentials and creates no agent. Only
    # the host below instantiates RestoredAgent or invokes assess().
    from src.intelligence.starship_restored_agent import validate_request
    row = validate_request(value)
    if row["schema"] == "missionos.starship_restored_decision_request.v2" and expected_source_sha256 is not None:
        _require(row["operational_intent"]["source_sha256"] == expected_source_sha256,
            "restored_intent_request_source_differs_from_pipe")
    window = row["deadline_s"]-row["time_s"]
    _require(BUDGETS["typesafe"] <= window <= WINDOWS[row["phase"]], "restored_pipe_unreserved_initial_window")
    return row


def _binding(context, plan_sha256, source_sha256, *, return_review_policy=None):
    _context(context)
    _require(_hash(plan_sha256) and _hash(source_sha256), "restored_pipe_invalid_plan_or_source")
    result = {"context": _copy(context), "plan_sha256": plan_sha256, "source_sha256": source_sha256}
    if return_review_policy is not None:
        result["return_review_policy_sha256"] = policy_digest(return_review_policy)
    return result


def _judgment(value, request, provider):
    _require(type(value) is dict and value.get("schema") == "missionos.starship_restored_judgment.v1"
        and value.get("request_id") == request["request_id"] and value.get("request_sha256") == _digest(request)
        and value.get("provider") == provider and value.get("execution_permission_granted") is False
        and value.get("status") in ("valid", "no_valid_judgment")
        and type(value.get("model_inference_invoked")) is bool
        and (value.get("status") != "valid" or value.get("action") in request["allowed_actions"])
        and (value.get("status") != "no_valid_judgment" or value.get("action") is None),
        "restored_pipe_invalid_agent_judgment")
    return _copy(value)


def _deep_request(original, remaining, *, return_review_policy=None):
    row = _copy(original)
    if return_review_policy is None or "request_deep_reasoning" in row["allowed_actions"]:
        row["allowed_actions"].remove("request_deep_reasoning")
    _require(bool(row["allowed_actions"]), "restored_pipe_no_delegated_actions")
    row["deadline_s"] = min(original["deadline_s"], original["time_s"]+remaining)
    return _request(row)


def _result(request, attempts, judgment=None, *, reason=None, return_review_policy=None):
    proof = None
    if return_review_policy is not None:
        judgment = attempts[-1].get("judgment") if attempts else None
        proof = review_route(request, attempts[0].get("judgment") if attempts else None, return_review_policy)
        if reason is None and proof["route"] == "invalid_jev_metrics":
            reason = "invalid_jev_review_metrics"
        if reason is None and proof["review_required"] and len(attempts) < 2:
            reason = "required_return_review_not_completed"
    valid = judgment is not None and judgment.get("status") == "valid" and judgment.get("action") != "request_deep_reasoning" and reason is None
    invoked = [row["judgment"]["model_inference_invoked"] for row in attempts if row.get("judgment") is not None]
    unknown = any(row.get("judgment") is None for row in attempts) or bool(reason and reason.startswith("original_deadline"))
    result = {"schema": EXCHANGE if return_review_policy is None else EXCHANGE_V2, "request_id": request["request_id"], "request_sha256": _digest(request),
        "status": "valid" if valid else "no_valid_judgment", "action": judgment["action"] if valid else None,
        "judgment": _copy(judgment), "attempts": _copy(attempts), "reason": reason,
        "model_inference_invoked": True if any(invoked) else None if unknown else False,
        "execution_permission_granted": False, "delegation_is_approval": False,
        "synthetic_transport_is_model_evidence": False}
    if proof is not None:
        result["return_review"] = proof
    return result


def _validate_result(result, original, *, return_review_policy=None):
    if return_review_policy is not None:
        validate_policy(return_review_policy)
    _require(type(result) is dict and result.get("schema") == (EXCHANGE if return_review_policy is None else EXCHANGE_V2)
        and result.get("request_id") == original["request_id"] and result.get("request_sha256") == _digest(original)
        and result.get("status") in ("valid", "no_valid_judgment")
        and result.get("execution_permission_granted") is False and result.get("delegation_is_approval") is False
        and result.get("synthetic_transport_is_model_evidence") is False
        and type(result.get("attempts")) is list and len(result["attempts"]) <= 2,
        "restored_pipe_invalid_exchange")
    attempts = result["attempts"]
    for index, row in enumerate(attempts):
        provider = "typesafe" if index == 0 else "deepseek"
        _require(row.get("provider") == provider and _number(row.get("remaining_s"))
            and _enough(provider, row["remaining_s"]) and row["remaining_s"] <= WINDOWS[original["phase"]],
            "restored_pipe_attempt_window_or_order")
        request = original
        if index:
            first = attempts[0].get("judgment")
            if return_review_policy is None:
                _require(original["phase"] == "return" and type(first) is dict
                    and first.get("status") == "valid" and first.get("action") == "request_deep_reasoning",
                    "restored_pipe_delegation_not_explicit")
            else:
                route = review_route(original, first, return_review_policy)
                _require(original["phase"] == "return" and route["review_required"] is True
                    and row["remaining_s"] <= attempts[0]["remaining_s"],
                    "restored_pipe_deep_review_not_required_or_epoch_extended")
            request = _deep_request(original, row["remaining_s"], return_review_policy=return_review_policy)
        _require(row.get("request_sha256") == _digest(request)
            and row.get("decision_deadline_s") == request["deadline_s"], "restored_pipe_attempt_request_changed")
        if row.get("judgment") is not None:
            _judgment(row["judgment"], request, provider)
        else:
            _require(type(row.get("error_type")) is str, "restored_pipe_missing_attempt_error")
    final = result.get("judgment")
    if return_review_policy is not None:
        _require(final == (attempts[-1].get("judgment") if attempts else None),
            "restored_pipe_final_judgment_not_last_attempt")
    _require(final is None or bool(attempts) and final == attempts[-1].get("judgment"), "restored_pipe_final_judgment_not_last_attempt")
    expected = _result(original, attempts, final, reason=result.get("reason"), return_review_policy=return_review_policy)
    _require(_wire(result) == _wire(expected), "restored_pipe_exchange_action_or_claim_changed")
    return _copy(result)


class RestoredDecisionClient:
    """Worker-only, nonblocking single-pending-request endpoint."""

    def __init__(self, *, context, plan_sha256, source_sha256, request_writer, response_reader, clock=time.monotonic,
            return_review_policy=None):
        _require(isinstance(request_writer, ApprovalPipeWriter) and isinstance(response_reader, ApprovalPipeReader) and callable(clock),
            "restored_decision_requires_inherited_pipes")
        self.return_review_policy = None if return_review_policy is None else validate_policy(return_review_policy)
        self.binding = _binding(context, plan_sha256, source_sha256, return_review_policy=self.return_review_policy)
        self.writer, self.reader, self.clock = request_writer, response_reader, clock
        self.sequence, self.pending, self.completed, self.seen, self.events = 0, None, [], set(), []
        self.retired = {}

    def begin(self, request, *, started_wall_s=None):
        row, now = _request(request, expected_source_sha256=self.binding["source_sha256"]), self.clock()
        _require(self.pending is None and row["request_id"] not in self.seen and _number(now),
            "restored_decision_duplicate_or_pending_request")
        # A caller may have spent part of this original epoch assembling the
        # request. Preserve that start before sending, without changing legacy
        # frames or starting a fresh window after the work has completed.
        started = now if started_wall_s is None else started_wall_s
        _require(_number(started) and started <= now
            and now < started+row["deadline_s"]-row["time_s"],
            "restored_decision_original_wall_epoch_invalid_or_expired")
        self.sequence += 1
        frame = {"schema": REQUEST_FRAME if self.return_review_policy is None else REQUEST_FRAME_V2, **self.binding, "sequence": self.sequence,
            "request_id": row["request_id"], "request_sha256": _digest(row), "request": row,
            "started_wall_s": started, "wall_deadline_s": started+row["deadline_s"]-row["time_s"]}
        _require(len(_wire(frame)) <= MAX_FRAME_BYTES, "restored_decision_request_frame_too_large")
        self.writer.send(frame)
        self.seen.add(row["request_id"])
        self.pending = {"frame": frame, "last_sim_s": row["time_s"]}
        self.events.append({"event": "request_sent", "sequence": self.sequence, "request_sha256": _digest(row)})
        return _copy(frame)

    def poll(self, current_sim_s):
        _require(_number(current_sim_s), "restored_decision_invalid_simulation_clock")
        self.writer.flush()
        response = self.reader.poll()
        # A timed-out request can have exactly one authenticated-by-binding
        # late host frame still in flight. Keep it as late evidence, never as
        # the next phase's answer. Unknown, repeated or normally completed
        # frames remain replay errors.
        while type(response) is dict and type(response.get("sequence")) is int and response["sequence"] in self.retired:
            previous = self.retired[response["sequence"]]
            _require(previous["awaiting_late_response"] is True,
                "restored_decision_unsolicited_or_replayed_response")
            old_frame = previous["frame"]
            late = self._response(response, old_frame)
            previous["awaiting_late_response"] = False
            self.events.append({"event": "retired_late_response", "sequence": old_frame["sequence"],
                "request_sha256": old_frame["request_sha256"], "late_response": late,
                "adopted": False})
            response = self.reader.poll()
        _require(self.pending is not None or response is None, "restored_decision_unsolicited_or_replayed_response")
        if self.pending is None:
            return None
        frame, request = self.pending["frame"], self.pending["frame"]["request"]
        _require(current_sim_s >= self.pending["last_sim_s"], "restored_decision_simulation_clock_regressed")
        self.pending["last_sim_s"] = current_sim_s
        if response is not None:
            result = self._response(response, frame)
        else:
            result = None
        if current_sim_s >= request["deadline_s"] or self.clock() >= frame["wall_deadline_s"]:
            self.events.append({"event": "original_deadline_expired", "request_sha256": frame["request_sha256"],
                "late_response": result})
            result = _result(request, [] if result is None else result["attempts"],
                None if result is None else result["judgment"], reason="original_deadline_expired",
                return_review_policy=self.return_review_policy)
        if result is not None:
            self.completed.append(_copy(result))
            self.retired[frame["sequence"]] = {"frame": _copy(frame),
                "awaiting_late_response": response is None and result.get("reason") == "original_deadline_expired"}
            self.pending = None
        return result

    def _response(self, response, frame):
        _require(type(response) is dict and set(response) == {"schema", *self.binding, "sequence", "request_id",
            "request_sha256", "result_sha256", "result"} and response["schema"] == (RESPONSE_FRAME if self.return_review_policy is None else RESPONSE_FRAME_V2)
            and all(response.get(key) == frame[key] for key in (*self.binding, "sequence", "request_id", "request_sha256"))
            and type(response["sequence"]) is int and response["result_sha256"] == _digest(response["result"]),
            "restored_decision_response_binding_mismatch")
        return _validate_result(response["result"], frame["request"], return_review_policy=self.return_review_policy)

    def record(self):
        return _copy({"schema": "missionos.starship_restored_decision_client.v1" if self.return_review_policy is None else CLIENT_V2,
            **self.binding, **({"return_review_policy": self.return_review_policy} if self.return_review_policy is not None else {}),
            "pending_request": self.pending, "results": self.completed, "events": self.events,
            "provider_calls_by_worker": 0, "execution_permission_granted": False})


class RestoredDecisionHost:
    """Nonblocking host pump; only its one thread per request invokes agents."""

    def __init__(self, *, context, plan_sha256, source_sha256, reader, writer, agent_factory=None,
            active=lambda: True, clock=time.monotonic, return_review_policy=None):
        _require(isinstance(reader, ApprovalPipeReader) and isinstance(writer, ApprovalPipeWriter)
            and callable(active) and callable(clock) and (agent_factory is None or callable(agent_factory)),
            "restored_decision_host_requires_inherited_pipes_and_callbacks")
        self.return_review_policy = None if return_review_policy is None else validate_policy(return_review_policy)
        self.binding = _binding(context, plan_sha256, source_sha256, return_review_policy=self.return_review_policy)
        self.reader, self.writer = reader, writer
        self.factory, self.active, self.clock = agent_factory, active, clock
        self.sequence, self.pending, self.sessions, self.seen, self.events = 0, None, [], set(), []

    def _result(self, request, attempts, judgment=None, *, reason=None):
        return _result(request, attempts, judgment, reason=reason, return_review_policy=self.return_review_policy)

    def _work(self, session):
        request, channel, deadline = session["frame"]["request"], session["queue"], session["frame"]["wall_deadline_s"]
        attempts = []
        try:
            if self.active() is not True or not _enough("typesafe", deadline-self.clock()):
                channel.put(("complete", self._result(request, attempts, reason="insufficient_initial_window")))
                return
            factory = self.factory
            if factory is None:
                from src.intelligence.starship_restored_agent import RestoredAgent
                factory = RestoredAgent
            agent = factory(active=lambda: self.active() is True and self.clock() < deadline, clock=self.clock)
            for provider in ("typesafe", "deepseek"):
                remaining = deadline-self.clock()
                if self.active() is not True or not _enough(provider, remaining):
                    reason = "insufficient_deep_reasoning_window" if provider == "deepseek" else "insufficient_initial_window"
                    channel.put(("complete", self._result(request, attempts, attempts[-1]["judgment"] if attempts else None, reason=reason)))
                    return
                selected = request if provider == "typesafe" else _deep_request(request, remaining, return_review_policy=self.return_review_policy)
                row = {"provider": provider, "remaining_s": remaining, "decision_deadline_s": selected["deadline_s"],
                    "request_sha256": _digest(selected), "judgment": None}
                channel.put(("started", {key: value for key, value in row.items() if key != "judgment"}))
                try:
                    row["judgment"] = _judgment(agent.assess(selected, provider=provider, remaining_s=remaining,
                        outer_deadline_monotonic=deadline), selected, provider)
                except Exception as error:
                    row["error_type"] = type(error).__name__
                attempts.append(row)
                channel.put(("attempt", _copy(row)))
                judgment = row["judgment"]
                if self.clock() >= deadline or self.active() is not True:
                    channel.put(("complete", self._result(request, attempts, judgment, reason="original_deadline_expired_or_inactive")))
                    return
                if judgment is None or judgment["status"] != "valid":
                    channel.put(("complete", self._result(request, attempts, judgment, reason="no_valid_provider_judgment")))
                    return
                if provider == "typesafe":
                    if self.return_review_policy is None:
                        if judgment["action"] == "request_deep_reasoning":
                            _require(request["phase"] == "return", "restored_delegation_only_in_return")
                            continue
                    else:
                        route = review_route(request, judgment, self.return_review_policy)
                        if route["route"] == "invalid_jev_metrics":
                            channel.put(("complete", self._result(request, attempts, judgment,
                                reason="invalid_jev_review_metrics")))
                            return
                        if route["review_required"]:
                            continue
                channel.put(("complete", self._result(request, attempts, judgment)))
                return
        except Exception as error:
            channel.put(("complete", self._result(request, attempts, reason="host_exception_"+type(error).__name__)))

    def _drain(self, session):
        while True:
            try:
                kind, value = session["queue"].get_nowait()
            except queue.Empty:
                return
            if kind == "started":
                session["started_attempts"].append(value)
            elif kind == "attempt":
                session["attempts"].append(value)
            else:
                session["thread_result"] = value

    def tick(self):
        flushed = self.writer.flush()
        for session in self.sessions:
            self._drain(session)
        if self.pending is not None:
            session = self.pending
            frame, result = session["frame"], session.get("thread_result")
            if self.clock() >= frame["wall_deadline_s"] or self.active() is not True:
                result = self._result(frame["request"], session["attempts"],
                    None if result is None else result["judgment"], reason="original_deadline_expired_or_inactive")
            if result is not None and flushed:
                response = {"schema": RESPONSE_FRAME if self.return_review_policy is None else RESPONSE_FRAME_V2, **self.binding,
                    **{key: frame[key] for key in ("sequence", "request_id", "request_sha256")},
                    "result": result, "result_sha256": _digest(result)}
                self.writer.send(response)
                session["delivered_result"] = _copy(result)
                self.pending = None
            return None
        if not flushed:
            return None
        frame = self.reader.poll()
        if frame is None:
            return None
        try:
            _require(type(frame) is dict and set(frame) == {"schema", *self.binding, "sequence", "request_id",
                "request_sha256", "request", "started_wall_s", "wall_deadline_s"}
                and frame["schema"] == (REQUEST_FRAME if self.return_review_policy is None else REQUEST_FRAME_V2) and all(frame.get(key) == value for key, value in self.binding.items())
                and type(frame["sequence"]) is int and frame["sequence"] == self.sequence+1,
                "restored_decision_host_frame_binding_mismatch")
            request, now = _request(frame["request"], expected_source_sha256=self.binding["source_sha256"]), self.clock()
            _require(frame["request_id"] == request["request_id"] and frame["request_sha256"] == _digest(request)
                and request["request_id"] not in self.seen and _number(frame["started_wall_s"])
                and _number(frame["wall_deadline_s"]) and frame["started_wall_s"] <= now < frame["wall_deadline_s"]
                and frame["wall_deadline_s"] == frame["started_wall_s"]+request["deadline_s"]-request["time_s"]
                and not any(row["thread"].is_alive() for row in self.sessions),
                "restored_decision_duplicate_replay_deadline_or_running_attempt")
            self.sequence, self.pending = frame["sequence"], {"frame": _copy(frame), "queue": queue.Queue(),
                "attempts": [], "started_attempts": []}
            self.seen.add(request["request_id"])
            self.sessions.append(self.pending)
            thread = threading.Thread(target=self._work, args=(self.pending,), daemon=True,
                name="restored-decision-"+request["request_id"])
            self.pending["thread"] = thread
            thread.start()
        except Exception as error:
            self.events.append({"event": "request_rejected", "reason": str(error)[:160]
                if isinstance(error, ValueError) else type(error).__name__})
            raise
        return None

    def record(self):
        for session in self.sessions:
            self._drain(session)
        return _copy({"schema": "missionos.starship_restored_decision_host.v1" if self.return_review_policy is None else HOST_V2,
            **self.binding, **({"return_review_policy": self.return_review_policy} if self.return_review_policy is not None else {}),
            "requests": [{"frame": row["frame"], "attempts": row["attempts"],
                "started_attempts": row["started_attempts"],
                "thread_result": row.get("thread_result"), "delivered_result": row.get("delivered_result")}
                for row in self.sessions], "events": self.events,
            "execution_permission_granted": False, "hosted_inference_requires_attempt_receipts": True})

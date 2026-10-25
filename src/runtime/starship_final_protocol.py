"""Bounded supplemental observation RPC over inherited pipes.

Only a trusted host handler may call providers. A reply is a proposal, never
approval or dispatch. The original wall epoch includes queue/handler time;
expired replies are retained without being applied. No retry occurs here.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import queue
import re
import threading
import time

from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, MAX_FRAME_BYTES

REQUEST = "missionos.starship_final_rpc_request.v1"
RESPONSE = "missionos.starship_final_rpc_response.v1"
DOMAINS = frozenset({"ascent", "booster", "visual", "sensor"})
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(wire(value)).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def is_hash(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def validate_observation(value, depth=0):
    """Finite framing bound; this alone is deliberately NOT a public projector."""
    require(depth <= 20, "final_rpc_observation_depth")
    if type(value) is dict:
        require(len(value) <= 256, "final_rpc_observation_members")
        for key, child in value.items():
            require(type(key) is str and 0 < len(key) <= 128, "final_rpc_observation_key")
            require(not any(word in key.lower() for word in
                ("true_", "truth", "fault_label", "fault_name", "physical_factor", "seed", "api_key", "secret", "token")),
                "final_rpc_private_truth_in_observation")
            validate_observation(child, depth+1)
    elif type(value) is list:
        require(len(value) <= 256, "final_rpc_observation_items")
        for child in value:
            validate_observation(child, depth+1)
    elif type(value) is str:
        require(len(value) <= 4096 and not re.search(r"/(?:Users|home)/", value),
            "final_rpc_private_or_unbounded_string")
    else:
        require(value is None or type(value) is bool or type(value) in (int, float)
            and math.isfinite(value), "final_rpc_observation_scalar")


def validate_public_observation(domain, value):
    """Require an explicit typed phase projection before a host sees a request.

    A key blacklist cannot establish that a numerical snapshot is publishable.
    These are finite allowlists, with deeper production semantic validators
    required again by the domain's host handler before a provider send.
    """
    require(type(value) is dict, "final_rpc_typed_public_observation_required")
    if domain == "sensor":
        from .starship_final_sensor_execution import validate_public_request
        checked = validate_public_request(value)
        require(wire(checked) == wire(value), "final_rpc_sensor_projection")
    elif domain in ("ascent", "booster"):
        common = {"time_s", "source_time_s", "available_time_s", "altitude_m",
            "ground_speed_mps", "vertical_speed_mps", "horizontal_speed_mps", "propellant_kg",
            "body_rate_rad_s", "tilt_deg", "dynamic_pressure_pa", "attitude_error_deg",
            "observed_available_engine_count", "expected_engine_count"}
        extra = ({"measured_acceleration_mps2", "expected_acceleration_mps2", "minimum_orbit_margin_m", "deployment_allowed"}
            if domain == "ascent" else {"distance_to_tower_m", "distance_to_splashdown_m",
                "recovery_control_available", "splashdown_feasible", "tower_ready"})
        allowed = {"continue_mission", "withhold_deployment"} if domain == "ascent" else {
            "splashdown", "inhibit_recovery", "request_deep_reasoning"}
        require(set(value) == {"schema", "scope_sha256", "facts", "allowed_actions"}
            and value["schema"] == "missionos.starship_final_supervision_observation.v1"
            and is_hash(value["scope_sha256"]) and type(value["facts"]) is dict
            and value["facts"] and value["facts"].keys() <= common | extra
            and all(type(x) is bool or type(x) in (int, float) and math.isfinite(x)
                for x in value["facts"].values())
            and type(value["allowed_actions"]) is list and value["allowed_actions"]
            and all(type(x) is str and x in allowed for x in value["allowed_actions"]),
            "final_rpc_supervision_projection")
    elif domain == "visual":
        from .starship_visual_contract import validate_request as visual_request
        from ..intelligence.starship_visual_agent import _telemetry, _prior
        require(set(value) == {"schema", "run_id", "plan_sha256", "source_sha256", "mode", "policy_sha256",
            "request", "request_sha256", "frame_refs", "started_wall_s", "wall_deadline_s", "envelope_sha256"}
            and value["schema"] == "missionos.starship_final_visual_request.v1"
            and all(is_hash(value[k]) for k in ("plan_sha256", "source_sha256", "policy_sha256", "request_sha256", "envelope_sha256"))
            and type(value["frame_refs"]) is list, "final_rpc_visual_projection")
        checked = visual_request(value["request"])
        _telemetry(checked["telemetry"])
        _prior(checked["prior_diagnosis"])
        prior = checked["prior_diagnosis"]
        if prior is not None:
            require(prior.keys() <= {"schema", "evidence_kind", "request_sha256", "request_id", "episode_id",
                "snapshot_time_s", "model_id", "question_contract_sha256", "manifest_sha256",
                "telemetry_relation", "recommended_action", "observation_choice", "probabilities", "confidence",
                "confidence_calibrated", "visibility", "deployment_visual_state", "evidence_frame_ids",
                "prior_diagnosis_id", "diagnosis_id"}, "final_rpc_visual_prior_projection")
            for container in ("probabilities", "confidence"):
                question_values = prior.get(container, {})
                require(type(question_values) is dict, "final_rpc_visual_prior_projection")
                for name, score in question_values.items():
                    require(name in {"visibility", "deployment_visual_state", "telemetry_relation", "recommended_action", "observation_choice"}
                        or re.fullmatch(r"frame_[0-3]_support", name), "final_rpc_visual_prior_projection")
                    if container == "confidence":
                        require(type(score) in (int, float) and 0 <= score <= 1., "final_rpc_visual_prior_projection")
                    else:
                        from ..intelligence.starship_visual_agent import (
                            VISIBILITY, VISUAL_STATE, RELATION, ACTION_EFFECTS, OBSERVATION_EFFECTS, FRAME_SUPPORT)
                        choices = set(VISIBILITY) | set(VISUAL_STATE) | set(RELATION) | set(ACTION_EFFECTS) | set(OBSERVATION_EFFECTS) | set(FRAME_SUPPORT)
                        require(type(score) is dict and score.keys() <= choices
                            and all(type(x) in (int, float) and 0 <= x <= 1. for x in score.values()),
                            "final_rpc_visual_prior_projection")
        require(wire(checked) == wire(value["request"]) and value["request_sha256"] == digest(checked)
            and len(value["frame_refs"]) == len(checked["manifest"]["frames"]), "final_rpc_visual_projection")
        for ref, frame in zip(value["frame_refs"], checked["manifest"]["frames"]):
            require(ref == {"frame_id": frame["frame_id"], "path": "frames/"+frame["image_sha256"]+".png",
                "sha256": frame["image_sha256"]}, "final_rpc_visual_frame_projection")
    else:
        raise ValueError("final_rpc_public_domain_required")


def request(request_id, domain, time_s, deadline_s, observation):
    require(type(request_id) is str and ID.fullmatch(request_id) and type(domain) is str and domain in DOMAINS
        and number(time_s) and number(deadline_s) and time_s < deadline_s <= time_s+180.,
        "final_rpc_request_identity_or_window")
    validate_observation(observation)
    validate_public_observation(domain, observation)
    if domain == "visual":
        require(number(observation["started_wall_s"]) and number(observation["wall_deadline_s"])
            and observation["request"]["deadline_s"] == deadline_s
            and time_s <= observation["request"]["snapshot_time_s"] < deadline_s
            and math.isclose(observation["wall_deadline_s"]-observation["started_wall_s"],
                deadline_s-time_s, rel_tol=0., abs_tol=1e-8),
            "final_rpc_visual_original_epoch")
    if domain == "sensor":
        require(observation["issued_at_s"] == time_s and observation["deadline_s"] == deadline_s
            and observation["request_id"] == request_id, "final_rpc_sensor_original_epoch")
    value = {"request_id": request_id, "domain": domain, "time_s": time_s,
        "deadline_s": deadline_s, "observation": deepcopy(observation)}
    require(len(wire(value)) < MAX_FRAME_BYTES-2048, "final_rpc_request_budget")
    return value


def validate_request(value):
    require(type(value) is dict and set(value) == {
        "request_id", "domain", "time_s", "deadline_s", "observation"}, "final_rpc_request_shape")
    return request(**value)


class FinalObservationClient:
    """One pending supplemental request; credentials/providers never enter here."""

    def __init__(self, writer, reader, *, plan_sha256, source_sha256, clock=time.monotonic):
        require(isinstance(writer, ApprovalPipeWriter) and isinstance(reader, ApprovalPipeReader)
            and is_hash(plan_sha256) and is_hash(source_sha256) and callable(clock),
            "final_rpc_inherited_binding_required")
        self.writer, self.reader, self.clock = writer, reader, clock
        self.binding = {"plan_sha256": plan_sha256, "source_sha256": source_sha256}
        self.sequence, self.pending, self.seen, self.completed = 0, None, set(), []
        self.retired, self.events = {}, []

    def begin(self, value, *, started_wall_s=None):
        value, now = validate_request(value), self.clock()
        start = now if started_wall_s is None else started_wall_s
        require(self.pending is None and value["request_id"] not in self.seen
            and number(now) and number(start) and start <= now
            and now < start+value["deadline_s"]-value["time_s"], "final_rpc_spent_or_expired_request")
        if value["domain"] == "visual":
            require(start == value["observation"]["started_wall_s"], "final_rpc_visual_original_epoch")
        frame = {"schema": REQUEST, **self.binding, "sequence": self.sequence+1,
            "request": value, "request_sha256": digest(value), "started_wall_s": start,
            "wall_deadline_s": start+value["deadline_s"]-value["time_s"]}
        if value["domain"] == "visual":
            require(frame["wall_deadline_s"] == value["observation"]["wall_deadline_s"],
                "final_rpc_visual_original_epoch")
        require(len(wire(frame)) <= MAX_FRAME_BYTES, "final_rpc_frame_budget")
        self.writer.send(frame)
        self.sequence += 1
        self.seen.add(value["request_id"])
        self.pending = {"frame": frame, "last_simulation_time_s": value["time_s"]}
        return deepcopy(frame)

    def poll(self, simulation_time_s):
        require(number(simulation_time_s), "final_rpc_simulation_clock")
        self.writer.flush()
        frame = self.reader.poll()
        while type(frame) is dict and type(frame.get("sequence")) is int and frame["sequence"] in self.retired:
            old = self.retired[frame["sequence"]]
            require(old["awaiting_late_response"] is True, "final_rpc_replayed_response")
            self._response(frame, old["frame"])
            old["awaiting_late_response"] = False
            self.events.append({"event": "retired_late_response", "response": deepcopy(frame), "adopted": False})
            frame = self.reader.poll()
        require(self.pending is not None or frame is None, "final_rpc_unsolicited_response")
        if self.pending is None:
            return None
        original = self.pending["frame"]
        require(number(simulation_time_s) and simulation_time_s >= self.pending["last_simulation_time_s"],
            "final_rpc_simulation_clock_regressed")
        self.pending["last_simulation_time_s"] = simulation_time_s
        expired = simulation_time_s >= original["request"]["deadline_s"] or self.clock() >= original["wall_deadline_s"]
        if frame is not None:
            self._response(frame, original)
        if frame is None and not expired:
            return None
        result = None if frame is None else deepcopy(frame["result"])
        record = {"frame": deepcopy(original), "response": deepcopy(frame), "expired": expired,
            "delivered_result": None if expired else result, "proposal_only": True,
            "execution_permission_granted": False}
        self.completed.append(record)
        self.retired[original["sequence"]] = {"frame": deepcopy(original), "awaiting_late_response": frame is None}
        self.pending = None
        return {"status": "expired", "action": None} if expired else result

    def _response(self, frame, original):
        require(type(frame) is dict and set(frame) == {"schema", *self.binding, "sequence", "request_sha256", "result", "result_sha256"}
            and frame["schema"] == RESPONSE and all(frame.get(k) == v for k, v in self.binding.items())
            and type(frame["sequence"]) is int and frame["sequence"] == original["sequence"]
            and frame["request_sha256"] == original["request_sha256"]
            and type(frame["result"]) is dict and frame["result_sha256"] == digest(frame["result"]),
            "final_rpc_response_binding")

    def record(self):
        return {"schema": "missionos.starship_final_rpc_client.v1", **self.binding,
            "completed": deepcopy(self.completed), "pending": deepcopy(self.pending),
            "events": deepcopy(self.events),
            "provider_calls_by_worker": 0, "execution_permission_granted": False}


class FinalObservationHost:
    """Source-bound host handler on one thread; no automatic retry or approval."""

    def __init__(self, reader, writer, *, plan_sha256, source_sha256, handler,
                 active=lambda: True, clock=time.monotonic):
        require(isinstance(reader, ApprovalPipeReader) and isinstance(writer, ApprovalPipeWriter)
            and is_hash(plan_sha256) and is_hash(source_sha256)
            and callable(handler) and callable(active) and callable(clock), "final_rpc_host_binding_required")
        self.reader, self.writer, self.handler, self.active, self.clock = reader, writer, handler, active, clock
        self.binding = {"plan_sha256": plan_sha256, "source_sha256": source_sha256}
        self.sequence, self.pending, self.seen, self.sessions = 0, None, set(), []

    def _work(self, session):
        frame = session["frame"]
        try:
            remaining = frame["wall_deadline_s"]-self.clock()
            require(self.active() is True and remaining > 0, "final_rpc_host_inactive_or_expired")
            result = self.handler(deepcopy(frame["request"]), remaining_s=remaining,
                outer_deadline_monotonic=frame["wall_deadline_s"])
            require(type(result) is dict and len(wire(result)) < MAX_FRAME_BYTES-2048,
                "final_rpc_host_result_budget")
            session["queue"].put({"result": deepcopy(result), "error_type": None})
        except Exception as error:
            session["queue"].put({"result": {"status": "unavailable", "action": None},
                "error_type": type(error).__name__})

    def tick(self):
        self._collect_answers()
        flushed = self.writer.flush()
        if self.pending is not None:
            session = self.pending
            try:
                session["answer"] = session["queue"].get_nowait()
            except queue.Empty:
                pass
            expired = self.clock() >= session["frame"]["wall_deadline_s"] or self.active() is not True
            if flushed and ("answer" in session or expired):
                result = {"status": "expired", "action": None} if expired else session["answer"]["result"]
                frame = {"schema": RESPONSE, **self.binding, "sequence": session["frame"]["sequence"],
                    "request_sha256": session["frame"]["request_sha256"], "result": result, "result_sha256": digest(result)}
                self.writer.send(frame)
                session["delivered_result"], session["expired"] = deepcopy(result), expired
                self.pending = None
            return
        if not flushed:
            return
        frame = self.reader.poll()
        if frame is None:
            return
        require(type(frame) is dict and set(frame) == {"schema", *self.binding, "sequence", "request", "request_sha256", "started_wall_s", "wall_deadline_s"}
            and frame["schema"] == REQUEST and all(frame.get(k) == v for k, v in self.binding.items())
            and type(frame["sequence"]) is int and frame["sequence"] == self.sequence+1,
            "final_rpc_host_frame_binding")
        value = validate_request(frame["request"])
        now = self.clock()
        require(value["request_id"] not in self.seen and frame["request_sha256"] == digest(value)
            and number(frame["started_wall_s"]) and number(frame["wall_deadline_s"])
            and frame["started_wall_s"] <= now < frame["wall_deadline_s"]
            and frame["wall_deadline_s"] == frame["started_wall_s"]+value["deadline_s"]-value["time_s"]
            and not any(row["thread"].is_alive() for row in self.sessions), "final_rpc_host_replayed_or_expired")
        if value["domain"] == "visual":
            require(frame["started_wall_s"] == value["observation"]["started_wall_s"]
                and frame["wall_deadline_s"] == value["observation"]["wall_deadline_s"],
                "final_rpc_visual_original_epoch")
        self.sequence = frame["sequence"]
        self.seen.add(value["request_id"])
        session = {"frame": deepcopy(frame), "queue": queue.Queue()}
        thread = threading.Thread(target=self._work, args=(session,), daemon=True,
            name="starship-final-observation-"+value["request_id"])
        session["thread"] = thread
        self.sessions.append(session)
        self.pending = session
        thread.start()

    def record(self):
        self._collect_answers()
        return {"schema": "missionos.starship_final_rpc_host.v1", **self.binding,
            "sessions": [{**{k: deepcopy(v) for k, v in row.items() if k not in ("thread", "queue")},
                "handler_finished": not row["thread"].is_alive()}
                for row in self.sessions], "execution_permission_granted": False}

    def _collect_answers(self):
        for session in self.sessions:
            if "answer" in session:
                continue
            try:
                session["answer"] = session["queue"].get_nowait()
                if session.get("expired") is True:
                    session["late_answer_adopted"] = False
            except queue.Empty:
                pass

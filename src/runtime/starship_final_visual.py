"""Opt-in same-flight visual observation; no inventory or plant writes.

The observer runs beside the numerical plant and calls only a nonblocking
host-RPC callback. The host owns VisualAgent/HTTP and reads hash-bound pixels.
Legacy VisualRuntime remains fixture-only. A model proposal never substitutes
for a fresh independent counter or the executor's numerical release gates.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from threading import Lock


SCHEMA = "missionos.starship_final_visual.v1"
ENVELOPE = "missionos.starship_final_visual_request.v1"
HOST_RESULT = "missionos.starship_final_visual_host_result.v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _require(value, code):
    if not value:
        raise ValueError(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class FinalVisualPolicy:
    trigger_released_count: int = 13
    first_counter_delay_s: float = 4.
    confirmation_timeout_s: float = 2.
    hold_s: float = 35.
    counter_max_age_s: float = 2.001
    frame_max_age_s: float = 3.
    apply_margin_s: float = 2.
    maximum_judgments: int = 2
    maximum_observations: int = 1
    width: int = 384
    height: int = 280

    def __post_init__(self):
        _require(all(_number(getattr(self, name)) for name in (
            "first_counter_delay_s", "confirmation_timeout_s", "hold_s",
            "counter_max_age_s", "frame_max_age_s", "apply_margin_s")), "final_visual_policy_numbers")
        _require(type(self.trigger_released_count) is int and self.trigger_released_count == 13
            and 0 < self.confirmation_timeout_s < self.first_counter_delay_s < self.hold_s <= 60.
            and 0 < self.counter_max_age_s <= 3. and self.frame_max_age_s == 3.
            and self.apply_margin_s == 2. and type(self.maximum_judgments) is int
            and self.maximum_judgments == 2 and type(self.maximum_observations) is int
            and self.maximum_observations == 1 and type(self.width) is int and type(self.height) is int
            and 256 <= self.width <= 640 and 256 <= self.height <= 640
            and 78400 <= self.width*self.height <= 640*640, "final_visual_policy_scope")

    def require_provider_dimensions(self):
        """Current executions must meet provider limits; old records stay readable."""
        from ..intelligence.starship_visual_transport import MIN_PROVIDER_IMAGE_SIDE
        _require(min(self.width, self.height) >= MIN_PROVIDER_IMAGE_SIDE,
                 "final_visual_provider_dimensions")
        return self

    def record(self):
        return {"schema": "missionos.starship_final_visual_policy.v1", **asdict(self),
            "counter_is_synthetic_delayed_observation": True,
            "images_cannot_assign_inventory": True, "automatic_retry": False}


@dataclass(frozen=True)
class FinalVisualPending:
    """The same existing host request is still pending; no new attempt."""


PENDING = FinalVisualPending()


def _binding(run_id, plan_sha256, source_sha256, mode, policy):
    _require(type(run_id) is str and _ID.fullmatch(run_id)
        and type(plan_sha256) is str and _HASH.fullmatch(plan_sha256)
        and type(source_sha256) is dict and source_sha256
        and all(type(k) is str and type(v) is str and _HASH.fullmatch(v)
            for k, v in source_sha256.items())
        and mode in ("fixture", "live") and type(policy) is FinalVisualPolicy,
        "final_visual_binding")
    return {"run_id": run_id, "plan_sha256": plan_sha256,
        "source_sha256": digest(source_sha256), "mode": mode,
        "policy_sha256": digest(policy.record())}


class FinalVisualObserver:
    """Actual-tick observer with a single bounded confirmation-delay episode.

    ``decide(envelope)`` returns PENDING/None while the original host request is
    in flight, or its bound FinalVisualHost result. The callback must not call
    providers in this worker. Call observe after each actual orbital-coast tick
    and honor release_allowed/withhold_deployment before another release.
    """

    def __init__(self, profile, output_dir, *, run_id, plan_sha256,
            source_sha256, mode, decide, policy=None, clock=time.monotonic):
        from .starship_anomaly_events import ConfirmationChannel
        from .starship_deployment_world import DeploymentWorld
        from .starship_visual_observation import VisualObservationCollector, default_cameras
        self.policy = (FinalVisualPolicy() if policy is None else policy).require_provider_dimensions()
        self.binding = _binding(run_id, plan_sha256, source_sha256, mode, self.policy)
        _require(callable(decide) and callable(clock), "final_visual_observer_callbacks")
        self.decide, self.clock, self.run_id = decide, clock, run_id
        self.channel = ConfirmationChannel(run_id, first_delay_s=self.policy.first_counter_delay_s)
        self.collector = VisualObservationCollector(DeploymentWorld(profile), output_dir,
            run_id=run_id, cameras=default_cameras(width=self.policy.width, height=self.policy.height),
            config={"max_age_s": self.policy.frame_max_age_s, "maximum_captures": 256,
                "maximum_pinned_frames": 12, "store_bytes": 16*1024**2, "ring_bytes": 8*1024**2})
        self._physical_count, self._clock, self._episode = 0, None, None
        self._pending, self._prior, self._after = None, None, None
        self._next_kind, self._next_camera = "visual", "deployment_port_primary"
        self._closed, self._stop = None, False
        self.requests, self.results, self.events, self.observations = [], [], [], []
        self._last_numeric = False

    @property
    def original_epoch_s(self):
        """The episode start survives every later camera/confirmation request."""
        _require(self._episode is not None and self._pending is not None,
            "final_visual_no_pending_episode_epoch")
        return self._episode["started_s"]

    @property
    def deployment_decision_ready(self):
        """Only the terminal episode may release the ordinary count13 judgment."""
        return self._closed is not None

    @property
    def withhold_deployment(self):
        return self._stop

    def release_allowed(self, now_s):
        _require(_number(now_s), "final_visual_release_clock")
        if self._stop or self._episode is not None and self._closed is None:
            return False
        return self.channel.covers_attempt(now_s, max_age_s=self.policy.counter_max_age_s,
            expected_increment=True) if self.channel.current_attempt else True

    def _finish(self, action, now, reason, *, basis="rules"):
        self._closed, self._pending = reason, None
        self._stop = action == "withhold_deployment"
        self.events.append({"event": "visual_episode_closed", "action": action,
            "time_s": now, "reason": reason, "basis": basis,
            "inventory_assigned": False, "plant_state_assigned": False})

    def observe(self, state, vehicle, satellites, *, phase="orbital_coast",
            numeric_release_ready=True, return_deadline_s):
        """Copy present observations, never return or install a replacement state."""
        now = state.time_s
        _require(_number(now) and (self._clock is None or now >= self._clock)
            and type(numeric_release_ready) is bool and _number(return_deadline_s),
            "final_visual_actual_clock_or_numeric_gate")
        self._clock, self._last_numeric = now, numeric_release_ready
        if phase != "orbital_coast":
            return None
        count = len(satellites)
        _require(count in (self._physical_count, self._physical_count+1), "final_visual_release_gap")
        if count != self._physical_count:
            _require(self.release_allowed(now), "final_visual_release_while_not_admitted")
            if count == self.policy.trigger_released_count or self.channel.current_attempt is not None:
                _require(self.channel.count == count-1, "final_visual_counter_not_primed_before_attempt")
                attempt = self.channel.attempt(now, count)
                self.collector.world.select_target(f"satellite_{count:02d}")
                self.events.append({"event": "release_ack_observed", **attempt,
                    "separation_inferred_from_ack": False})
            self._physical_count = count
        self.channel.observe(now, count)
        self.collector.world.observe_plant(state, vehicle, satellites, now=now)
        if self.channel.current_attempt is None:
            return None
        attempt = self.channel.current_attempt
        if self._closed is None:
            self.collector.capture(now)
        if self._episode is None and now >= attempt["source_time_s"]+self.policy.confirmation_timeout_s:
            if not self.channel.covers_attempt(now, max_age_s=self.policy.counter_max_age_s, expected_increment=True):
                wall = self.clock()
                end = min(now+self.policy.hold_s, return_deadline_s)
                _require(_number(wall) and end > now+self.policy.apply_margin_s,
                    "final_visual_episode_no_headroom")
                self._episode = {"episode_id": "visual-"+attempt["release_attempt_id"],
                    "event_id": "unconfirmed-"+attempt["release_attempt_id"],
                    "started_s": now, "deadline_s": end, "started_wall_s": wall,
                    "wall_deadline_s": wall+end-now, "release_attempt_id": attempt["release_attempt_id"]}
                self.events.append({"event": "precautionary_hold", "time_s": now,
                    "origin": "rules", "symptom": "ack_without_arrived_separation_confirmation"})
        if self._episode is None or self._closed is not None:
            return None
        if (now+self.policy.apply_margin_s >= self._episode["deadline_s"]
                or self.clock()+self.policy.apply_margin_s >= self._episode["wall_deadline_s"]):
            self._finish("withhold_deployment", now, "original_deadline_expired")
            return "withhold_deployment"
        if self._pending is None:
            self._issue(now, state.propellant_kg, return_deadline_s)
        if self._pending is None:
            return None
        reply = self.decide(_copy(self._pending))
        if reply is None or type(reply) is FinalVisualPending:
            return None
        self._receive(reply, now)
        return "withhold_deployment" if self._stop else None

    def _issue(self, now, fuel, return_deadline):
        from .starship_visual_contract import validate_request
        if len(self.requests) >= self.policy.maximum_judgments:
            self._finish("withhold_deployment", now, "judgment_budget_exhausted")
            return
        if self._after is not None:
            if self._next_kind == "numeric_followup":
                if self.channel.latest is None or self.channel.latest["source_time_s"] <= self._after:
                    return
            else:
                self.collector.request_observation(self._next_camera.replace(
                    "deployment_port_secondary", "alternate_camera").replace("deployment_port_primary", "next_frame_primary"), now)
        # The final bounded judgment receives both its requested new observation
        # and the arrived independent counter. Do not spend that last judgment
        # in the short registered transport gap with only stop available.
        if self._after is not None and not self.channel.covers_attempt(now,
                max_age_s=self.policy.counter_max_age_s, expected_increment=True):
            return
        manifest = self.collector.evidence(now, camera_id=self._next_camera, maximum_frames=3,
            after_capture_s=self._after if self._next_kind == "visual" else None)
        if self._next_kind == "visual" and not manifest["frames"]:
            return
        if self._next_kind == "numeric_followup":
            manifest["frames"] = []
            manifest["manifest_sha256"] = digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        fresh = self.channel.covers_attempt(now, max_age_s=self.policy.counter_max_age_s, expected_increment=True)
        actions, choices = ["stop_deployment"], ["none"]
        if fresh and self._last_numeric:
            actions.insert(0, "continue")
        if (self._next_kind == "visual"
                and len(self.observations) < self.policy.maximum_observations):
            actions += ["request_visual_observation", "request_numeric_confirmation"]
            choices += ["alternate_camera", "next_frame_primary", "fresh_separation_counter"]
        latest, attempt = self.channel.latest, self.channel.current_attempt
        request = {"schema": "missionos.starship_visual_request.v1", "run_id": self.run_id,
            "request_id": self._episode["episode_id"]+"-"+str(len(self.requests)+1),
            "episode_id": self._episode["episode_id"], "vehicle_id": "ship",
            "release_attempt_id": attempt["release_attempt_id"], "event_id": self._episode["event_id"],
            "authority_sha256": digest(self.binding), "state_generation": attempt["expected_count"],
            "evidence_kind": self._next_kind, "snapshot_time_s": now,
            "deadline_s": self._episode["deadline_s"], "manifest": manifest,
            "telemetry": {"time_s": now, "phase": "orbital_coast", "sequencer_state": "held",
                "release_acknowledged": attempt["acknowledged"], "last_confirmed_count": self.channel.count,
                "counter_source_time_s": latest["source_time_s"] if latest else None,
                "counter_available_time_s": latest["available_time_s"] if latest else None,
                "counter_source_sequence": latest["source_sequence"] if latest else None,
                "inventory_fresh": fresh, "release_confirmation_valid": fresh,
                "expected_count": attempt["expected_count"], "release_attempt_id": attempt["release_attempt_id"],
                "return_deadline_s": return_deadline, "fuel_kg": fuel,
                "orbit_release_feasible": self._last_numeric, "hold_expires_at_s": self._episode["deadline_s"]},
            "allowed_actions": actions, "observation_choices": choices, "prior_diagnosis": self._prior}
        validate_request(request)
        frame_refs = [{"frame_id": f["frame_id"], "path": "frames/"+f["image_sha256"]+".png",
            "sha256": f["image_sha256"]} for f in manifest["frames"]]
        envelope = {"schema": ENVELOPE, **self.binding, "request": request,
            "request_sha256": digest(request), "frame_refs": frame_refs,
            "started_wall_s": self._episode["started_wall_s"],
            "wall_deadline_s": self._episode["wall_deadline_s"]}
        envelope["envelope_sha256"] = digest(envelope)
        self._pending = _copy(envelope)
        self.requests.append(_copy(envelope))

    def _receive(self, reply, now):
        envelope = self._pending
        try:
            _require(now+self.policy.apply_margin_s < self._episode["deadline_s"]
                and self.clock()+self.policy.apply_margin_s < self._episode["wall_deadline_s"],
                "final_visual_result_deadline")
            _require(type(reply) is dict and set(reply) == {"schema", *self.binding,
                "envelope_sha256", "request_sha256", "result", "result_sha256"}
                and reply["schema"] == HOST_RESULT
                and all(reply[k] == v for k, v in self.binding.items())
                and reply["envelope_sha256"] == envelope["envelope_sha256"]
                and reply["request_sha256"] == envelope["request_sha256"]
                and reply["result_sha256"] == digest(reply["result"]), "final_visual_unbound_host_result")
            validate_result(reply["result"], envelope["request"], self.binding["mode"])
            action, choice = reply["result"]["action"], reply["result"]["observation_choice"]
            fresh = self.channel.covers_attempt(now, max_age_s=self.policy.counter_max_age_s, expected_increment=True)
            _require(action != "continue" or fresh and self._last_numeric, "final_visual_resume_without_fresh_numeric_evidence")
        except (ValueError, TypeError, KeyError):
            # No arbitrary rejected body is copied into evidence. Preserve only a
            # finite diagnostic plus bound metadata from an actual host result.
            rejected = ({"schema": HOST_RESULT if reply.get("schema") == HOST_RESULT else None,
                "request_sha256": reply.get("request_sha256") if type(reply.get("request_sha256")) is str
                    and _HASH.fullmatch(reply["request_sha256"]) else None}
                if type(reply) is dict else None)
            if type(reply) is dict and type(reply.get("result")) is dict:
                receipt = reply["result"].get("provider_receipt")
                if type(receipt) is dict:
                    from ..intelligence.starship_visual_transport import REJECTION_STAGES
                    category = receipt.get("rejection_category")
                    if type(category) is str and category in {*REJECTION_STAGES, "response_json_or_structure"}:
                        rejected["provider_rejection_category"] = category
                    diagnostic = receipt.get("rejection_diagnostics")
                    if type(diagnostic) is dict:
                        safe = {"schema": "missionos.starship_visual_rejection_diagnostics.v1"}
                        if type(diagnostic.get("stage")) is str and diagnostic["stage"] in {
                                "response_envelope", "answers", "choice", "semantics", "response_json", "semantic_decode"}:
                            safe["stage"] = diagnostic["stage"]
                        for key in ("expected_question_count", "received_question_count", "missing_question_count",
                                "unexpected_question_count", "question_index", "response_model_utf8_bytes"):
                            value = diagnostic.get(key)
                            if value is None or type(value) is int and 0 <= value <= 10**9:
                                safe[key] = value
                        if type(diagnostic.get("response_model_matches")) is bool:
                            safe["response_model_matches"] = diagnostic["response_model_matches"]
                        for key in ("response_json_shape", "response_model_shape", "answers_json_shape"):
                            if type(diagnostic.get(key)) is str and diagnostic[key] in {"object", "array", "string", "number", "boolean", "null", "other"}:
                                safe[key] = diagnostic[key]
                        if type(diagnostic.get("response_model_sha256")) is str and _HASH.fullmatch(diagnostic["response_model_sha256"]):
                            safe["response_model_sha256"] = diagnostic["response_model_sha256"]
                        rejected["provider_rejection_diagnostics"] = safe
            self.results.append({"envelope_sha256": envelope["envelope_sha256"], "reply": rejected,
                "accepted": False, "time_s": now, "rejection": "invalid_or_unadmitted_visual_result"})
            self._finish("withhold_deployment", now, "invalid_visual_result_no_retry")
            return
        self.results.append({"envelope_sha256": envelope["envelope_sha256"], "reply": _copy(reply),
            "accepted": True, "time_s": now, "rejection": None})
        self._pending = None
        if action in ("continue", "stop_deployment"):
            self._finish("continue_deployment" if action == "continue" else "withhold_deployment", now,
                "accepted_visual_proposal", basis="ai" if self.binding["mode"] == "live" else "fixture")
        else:
            self._prior, self._after = _copy(reply["result"]["visual_diagnosis"]), now
            self._next_kind = "numeric_followup" if action == "request_numeric_confirmation" else "visual"
            self._next_camera = "deployment_port_secondary" if choice == "alternate_camera" else "deployment_port_primary"
            self.observations.append({"requested_at_s": now, "action": action, "choice": choice,
                "request_sha256": envelope["request_sha256"]})

    def record(self):
        native = sum(r["accepted"] and r["reply"]["result"]["model_inference_invoked"] is True for r in self.results)
        return _copy({"schema": SCHEMA, **self.binding, "policy": self.policy.record(),
            "episode": self._episode, "requests": self.requests, "results": self.results,
            "observation_requests": self.observations, "events": self.events,
            "counter_attempts": self.channel.attempts, "counter_receipts": self.channel.receipts,
            "closed_reason": self._closed, "withhold_deployment": self._stop,
            "pending_request": self._pending, "native_judgments": native,
            "ai_visual_work_completed": bool(self.binding["mode"] == "live" and native
                and self._closed == "accepted_visual_proposal"),
            "inventory_assigned": False, "plant_state_assigned": False, "automatic_retry": False})


def validate_result(result, request, mode):
    """Strictly distinguish real provider receipts from local fixture evidence."""
    from ..intelligence.starship_visual_transport import JEV_MODEL
    from ..intelligence.starship_visual_agent import question_contract, decode_response
    _require(mode in ("live", "fixture"), "final_visual_result_mode")
    _require(type(result) is dict and result.get("schema") == "missionos.starship_visual_result.v1"
        and all(result.get(k) == request[k] for k in ("run_id", "request_id", "episode_id"))
        and result.get("request_sha256") == digest(request)
        and result.get("action") in request["allowed_actions"]
        and result.get("observation_choice") in request["observation_choices"], "final_visual_result_binding")
    receipt, diagnosis = result.get("provider_receipt"), result.get("visual_diagnosis")
    live = mode == "live"
    _require(type(receipt) is dict and type(diagnosis) is dict
        and result.get("status") == ("valid" if live else "fixture_valid")
        and result.get("model_inference_invoked") is live
        and receipt.get("status") == ("succeeded" if live else "fixture_succeeded")
        and receipt.get("transport_mode") == ("live" if live else "loopback_fixture")
        and receipt.get("provider") == ("typesafe" if live else "loopback_fixture")
        and all(receipt.get(k) is live for k in ("hosted_model_attempted", "call_attempted", "call_succeeded", "model_inference_invoked"))
        and receipt.get("local_http_attempted") is not live
        and receipt.get("response_received") is True and receipt.get("complete_response_observed") is True
        and receipt.get("semantic_request_sha256") == digest(request)
        and receipt.get("manifest_sha256") == digest(request["manifest"])
        and receipt.get("ordered_frame_sha256") == [f["image_sha256"] for f in request["manifest"]["frames"]]
        and receipt.get("requested_model_id") == receipt.get("model_id") == JEV_MODEL
        and receipt.get("retries") == 0 and receipt.get("redirects") == 0
        and all(receipt.get(k) is False for k in ("approval_granted", "dispatch", "execution_authorized", "physical_execution")),
        "final_visual_provider_receipt")
    _require(diagnosis.get("schema") == "missionos.starship_visual_diagnosis.v1"
        and diagnosis.get("request_sha256") == digest(request)
        and diagnosis.get("manifest_sha256") == digest(request["manifest"])
        and diagnosis.get("question_contract_sha256") == digest(question_contract(request))
        and diagnosis.get("diagnosis_id") == digest({k: v for k, v in diagnosis.items() if k != "diagnosis_id"})
        and diagnosis.get("recommended_action") == result["action"]
        and diagnosis.get("observation_choice") == result["observation_choice"]
        and diagnosis.get("model_id") == JEV_MODEL, "final_visual_diagnosis_binding")
    # Re-run the strict finite Choice decoder on the retained structured facts.
    # The diagnosis itself cannot gain unsupported findings or arbitrary fields
    # merely by recomputing its hash after the host has returned.
    questions = question_contract(request)
    answers = {}
    frame_ids = [f["frame_id"] for f in request["manifest"]["frames"]]
    support = diagnosis.get("evidence_frame_ids", [])
    _require(type(support) is list and len(support) == len(set(support))
        and all(type(value) is str and value in frame_ids for value in support),
        "final_visual_diagnosis_frame_support")
    for name in questions:
        selected = ("supports" if frame_ids[int(name.split("_")[1])] in support else "uncertain") if name.startswith("frame_") else diagnosis[name]
        answers[name] = {"type": "choice", "choice": selected,
            "probabilities": diagnosis["probabilities"][name], "confidence": diagnosis["confidence"][name]}
    _require(decode_response({"model": JEV_MODEL, "answers": answers}, request) == diagnosis,
        "final_visual_diagnosis_redecode")
    action, choice = result["action"], result["observation_choice"]
    _require((action == "request_visual_observation" and choice in ("alternate_camera", "next_frame_primary")
        or action == "request_numeric_confirmation" and choice == "fresh_separation_counter"
        or action in ("continue", "stop_deployment") and choice == "none"), "final_visual_action_observation_pair")
    return _copy(result)


class FinalVisualHost:
    """Host-only synchronous assessment, to be run in the parent's RPC thread.

    Frame references are fixed content-addressed filenames, opened with the
    production no-follow bounded reader. Provider input contains pixels and
    approved telemetry, never paths, scene truth, a fault schedule or source.
    Invalid/replayed input never receives an automatic second send.
    """

    def __init__(self, frame_root, *, run_id, plan_sha256, source_sha256, mode,
            policy=None, fixture_endpoint=None, live_authorized=lambda: False,
            active=lambda: True, clock=time.monotonic, ledger_path=None):
        self.policy = (FinalVisualPolicy() if policy is None else policy).require_provider_dimensions()
        self.binding = _binding(run_id, plan_sha256, source_sha256, mode, self.policy)
        self.root = Path(frame_root).absolute()
        _require(self.root.is_dir() and callable(active) and callable(clock)
            and callable(live_authorized), "final_visual_host_configuration")
        self.source = _copy(source_sha256)
        self.fixture_endpoint, self.live_authorized, self.active, self.clock = fixture_endpoint, live_authorized, active, clock
        self.seen, self.attempts = set(), []
        self._transport, self._lock = None, Lock()
        self._ledger = None
        if ledger_path is not None:
            from ..intelligence.starship_visual_transport import VisualAttemptLedger
            self._ledger = VisualAttemptLedger(ledger_path, run_id=run_id,
                authority_sha256=digest(self.binding),
                mode="live" if mode == "live" else "loopback_fixture",
                maximum_calls=self.policy.maximum_judgments)

    def assess(self, envelope):
        envelope = _copy(envelope)
        _require(self._lock.acquire(blocking=False), "final_visual_concurrent_request")
        try:
            return self._assess(envelope)
        finally:
            self._lock.release()

    def _assess(self, envelope):
        from .starship_visual_contract import validate_request
        from .starship_visual_observation import validate_frame, _check_pixels, default_cameras
        from .starship_restored_native_host import _read_artifact
        from ..intelligence.starship_visual_agent import VisualAgent
        from ..intelligence.starship_visual_transport import VisualTransport
        _require(type(envelope) is dict and set(envelope) == {"schema", *self.binding,
            "request", "request_sha256", "frame_refs", "started_wall_s", "wall_deadline_s", "envelope_sha256"}
            and envelope["schema"] == ENVELOPE and all(envelope[k] == v for k, v in self.binding.items())
            and envelope["envelope_sha256"] == digest({k: v for k, v in envelope.items() if k != "envelope_sha256"}),
            "final_visual_host_envelope_binding")
        request = validate_request(envelope["request"])
        _require(request["run_id"] == self.binding["run_id"] and request["authority_sha256"] == digest(self.binding)
            and envelope["request_sha256"] == digest(request)
            and _number(envelope["started_wall_s"]) and _number(envelope["wall_deadline_s"])
            and envelope["started_wall_s"] <= self.clock() < envelope["wall_deadline_s"]
            and 0 < envelope["wall_deadline_s"]-envelope["started_wall_s"] <= self.policy.hold_s
            and self.active() is True and request["request_id"] not in self.seen
            and len(self.seen) < self.policy.maximum_judgments,
            "final_visual_host_replay_deadline_or_budget")
        self.seen.add(request["request_id"])
        frames = request["manifest"]["frames"]
        _require(type(envelope["frame_refs"]) is list and len(envelope["frame_refs"]) == len(frames),
            "final_visual_host_frame_refs")
        camera_hashes = {c["camera_id"]: digest(c) for c in default_cameras(width=self.policy.width, height=self.policy.height)}
        checked = {}
        for frame, ref in zip(frames, envelope["frame_refs"]):
            validate_frame(frame, now=request["snapshot_time_s"], run_id=self.binding["run_id"], vehicle_id="ship", max_age_s=3.)
            _require(ref == {"frame_id": frame["frame_id"], "path": "frames/"+frame["image_sha256"]+".png",
                "sha256": frame["image_sha256"]} and frame["camera_config_sha256"] == camera_hashes.get(frame["camera_id"])
                and frame["renderer_source_sha256"] == self.source.get("src/runtime/starship_visual_observation.py"),
                "final_visual_host_unregistered_frame")
            raw = _read_artifact(self.root, ref["path"], 256*1024)
            _require(sha256(raw).hexdigest() == ref["sha256"], "final_visual_host_pixel_hash")
            _check_pixels(raw, frame)
            checked[frame["frame_id"]] = (deepcopy(frame), raw)
        class Pixels:
            def read(self, frame):
                _require(frame["frame_id"] in checked and checked[frame["frame_id"]][0] == frame,
                    "final_visual_host_unregistered_frame")
                return checked[frame["frame_id"]][1]
        self._current = envelope
        if self._transport is None:
            self._transport = VisualTransport(mode="live" if self.binding["mode"] == "live" else "loopback_fixture",
                fixture_endpoint=self.fixture_endpoint, maximum_calls=self.policy.maximum_judgments,
                ledger=self._ledger, live_authorized=self.live_authorized,
                active=lambda: self.active() is True and self.clock() < self._current["wall_deadline_s"],
                remaining=lambda: max(0., self._current["wall_deadline_s"]-self.clock()))
        result = VisualAgent(transport=self._transport, frame_store=Pixels()).assess(request)
        response = {"schema": HOST_RESULT, **self.binding, "envelope_sha256": envelope["envelope_sha256"],
            "request_sha256": digest(request), "result": result, "result_sha256": digest(result)}
        self.attempts.append({"envelope_sha256": envelope["envelope_sha256"],
            "request_sha256": digest(request), "response": _copy(response)})
        return response

    def record(self):
        return _copy({"schema": "missionos.starship_final_visual_host.v1", **self.binding,
            "attempts": self.attempts, "seen_request_ids": sorted(self.seen),
            "automatic_retry": False, "inventory_or_execution_authority_granted": False})

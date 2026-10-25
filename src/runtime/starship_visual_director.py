"""Opt-in visual episode actor; simulator remains the only physical executor.

Rules precautionary holds, fixture/model judgments, applied operations and
later observations are separate receipts. The historical Director contract is
not widened. Visual evidence cannot write the inventory or clear an interlock.
"""
from copy import deepcopy
from threading import Thread
import time

from .starship_anomaly_events import ConfirmationChannel, ReleaseConfirmationDetector
from .starship_deployment_forecast import ObservedDeploymentDirector
from .starship_visual_contract import digest, validate_authority, validate_request


class VisualRuntime:
    def __init__(self, *, run_id, authority, collector=None, agent=None):
        self.run_id, self.authority = run_id, validate_authority(authority)
        self.config = self.authority["visual"]
        self.authority_sha256 = digest(self.authority)
        self.collector, self.agent = collector, agent
        self.channel = ConfirmationChannel(run_id, first_delay_s=self.config["counter_delay_s"])
        self.detector = ReleaseConfirmationDetector(run_id,
            confirmation_timeout_s=self.config["confirmation_timeout_s"], authority_sha256=self.authority_sha256)
        self.director, self.episode = None, None
        self.requests, self.results, self.commands, self.observation_requests, self.observation_results = [], [], [], [], []
        self.superseded, self.waiting_release_gates = [], []
        self.pending, self._thread, self._answer = None, None, None
        self._pace_origin, self._next_kind, self._observation_due = None, "visual", None
        self._last_row, self._last_command_time_s, self._physical_count = None, None, 0
        self._clock, self._awaiting_applied, self._first_camera_request_time_s = None, False, None
        self._closed_reason, self._prior_diagnosis = None, None
        self._world_observed = False
        self._budget_stop_applied = False

    @property
    def observed_count(self):
        return self.channel.count

    @property
    def owns_deployment(self):
        return self.episode is not None

    @property
    def generation(self):
        return self.director.tracker.generation if self.director is not None else 0

    def on_attempt(self, time_s, physical_count, acknowledged=True, interlocked=False):
        self._physical_count = physical_count
        attempt = self.channel.attempt(time_s, physical_count, acknowledged=acknowledged)
        if self.collector is not None:
            self.collector.world.select_target(f"satellite_{attempt['expected_count']:02d}")
            if self.config["case"] == "missing_frames":
                self.collector.set_dropout(time_s)
        return attempt

    def observe_plant(self, state, vehicle, satellites, phase, sequencer_state):
        """Trusted sensor ingress. Truth is never copied into public requests."""
        now = state.time_s
        if self._clock is not None and now < self._clock:
            raise ValueError("visual_clock_reversed")
        self._clock = now
        if phase != "orbital_coast":
            return
        self._physical_count = len(satellites)
        self.channel.observe(now, self._physical_count)
        if self.collector is not None:
            self.collector.world.observe_plant(state, vehicle, satellites, now=now)
            self._world_observed = True
            self.collector.capture(now)

    def pace(self, now):
        if self.episode is None or self._closed_reason is not None or self._pace_origin is None:
            return
        start_sim, start_wall = self._pace_origin
        delay = (now-start_sim)-(time.monotonic()-start_wall)
        if delay > 0:
            time.sleep(min(delay, .25))

    def release_allowed(self, now):
        if self.episode is not None and self._closed_reason is None:
            return False
        allowed = (self.channel.current_attempt is None or self.channel.covers_attempt(now,
            max_age_s=self.config["counter_source_max_age_s"], expected_increment=True))
        if not allowed and (not self.waiting_release_gates or self.waiting_release_gates[-1]["time_s"] != now):
            self.waiting_release_gates.append({"time_s": now, "origin": "rules", "reason": "previous_attempt_unconfirmed"})
        return allowed

    def _fresh(self, now, *, increment=False):
        return self.channel.covers_attempt(now, max_age_s=self.config["counter_source_max_age_s"], expected_increment=increment)

    def release_conditions_met(self, row):
        return "continue" in self._allowed(row, followup=True)[0]

    def remaining_s(self):
        if self.episode is None or self._clock is None or self._closed_reason is not None:
            return 0.
        simulated = self.episode["deadline_s"]-self._clock
        start_sim, start_wall = self._pace_origin
        wall = self.episode["deadline_s"]-start_sim-(time.monotonic()-start_wall)
        return max(0., min(simulated, wall))

    def request_active(self):
        return self.pending is not None and self.remaining_s() > 0

    def _telemetry(self, row):
        receipt, attempt = self.channel.latest, self.channel.current_attempt
        return {"time_s": row["time_s"], "phase": row["phase"], "sequencer_state": row["sequencer_state"],
                "release_acknowledged": row["release_acknowledged"], "last_confirmed_count": self.observed_count,
                "counter_source_time_s": receipt["source_time_s"] if receipt else None,
                "counter_available_time_s": receipt["available_time_s"] if receipt else None,
                "counter_source_sequence": receipt["source_sequence"] if receipt else None,
                "inventory_fresh": self._fresh(row["time_s"]),
                "release_confirmation_valid": self._fresh(row["time_s"], increment=True),
                "expected_count": attempt["expected_count"] if attempt else None,
                "release_attempt_id": attempt["release_attempt_id"] if attempt else None,
                "return_deadline_s": row["return_deadline_s"], "fuel_kg": row["fuel_kg"],
                "orbit_release_feasible": row["numerical_tools"]["orbit_release_feasible"],
                "hold_expires_at_s": self.episode["deadline_s"] if self.episode else None}

    def _manifest(self, now, *, numeric=False):
        if numeric or self.collector is None:
            result = {"schema": "missionos.starship_visual_evidence.v1", "run_id": self.run_id,
                      "vehicle_id": "ship", "snapshot_time_s": now, "frames": []}
            result["manifest_sha256"] = digest(result)
            return result
        camera = self.config["camera_ids"][0]
        kwargs = {}
        if self.observation_requests and self.observation_requests[-1]["choice"] in ("alternate_camera", "next_frame_primary"):
            camera = self.config["camera_ids"][1] if self.observation_requests[-1]["choice"] == "alternate_camera" else camera
            kwargs["after_capture_s"] = self.observation_requests[-1]["requested_at_s"]
        return self.collector.evidence(now, camera_id=camera, maximum_frames=self.config["maximum_frames"], **kwargs)

    def _allowed(self, row, *, followup):
        actions = ["stop_deployment"]
        if (row["sequencer_state"] in ("running", "held") and self._fresh(row["time_s"], increment=True)
                and row["numerical_tools"]["orbit_release_feasible"]
                and row["fuel_kg"]-100. >= self.authority["m2_authority"]["deployment_authority"]["minimum_return_fuel_kg"]):
            actions.insert(0, "continue")
        choices = ["none"]
        if not followup and len(self.observation_requests) < self.config["maximum_observations"]:
            actions += ["request_visual_observation", "request_numeric_confirmation"]
            choices += ["alternate_camera", "next_frame_primary", "fresh_separation_counter"]
        return actions, choices

    def _issue(self, row):
        now, episode = row["time_s"], self.episode
        if len(self.requests) >= self.config["maximum_judgments"] or episode["deadline_s"]-now <= self.config["apply_margin_s"]:
            return
        numeric = self._next_kind == "numeric_followup"
        manifest = self._manifest(now, numeric=numeric)
        # The simulator captures before executing release on a tick. A frame
        # with the same timestamp can therefore still show pre-release state.
        if not numeric and not any(frame["capture_sim_time_s"] > self.channel.current_attempt["source_time_s"]
                                   for frame in manifest.get("frames", [])):
            episode["evidence_status"] = "no_arrived_fresh_frames"
            return
        actions, choices = self._allowed(row, followup=bool(self.requests))
        request = {"schema": "missionos.starship_visual_request.v1", "run_id": self.run_id,
                   "request_id": episode["episode_id"]+"-"+str(len(self.requests)+1),
                   "episode_id": episode["episode_id"], "vehicle_id": "ship",
                   "release_attempt_id": episode["event"]["release_attempt_id"], "event_id": episode["event"]["event_id"],
                   "authority_sha256": self.authority_sha256, "state_generation": self.generation,
                   "evidence_kind": self._next_kind, "snapshot_time_s": now,
                   "deadline_s": episode["deadline_s"], "manifest": manifest,
                   "telemetry": self._telemetry(row), "allowed_actions": actions, "observation_choices": choices,
                   "prior_diagnosis": deepcopy(self._prior_diagnosis)}
        validate_request(request)
        self.requests.append(deepcopy(request))
        self.pending, self._answer = request, None
        # The finite actor deadline remains authoritative even if a transport
        # worker fails to return; no automatic retry creates another attempt.
        def invoke():
            try:
                self._answer = self.agent.assess(deepcopy(request)) if self.agent is not None else {"status": "agent_unavailable"}
            except Exception as error:
                self._answer = {"status": "agent_failed", "error_type": type(error).__name__}
        self._thread = Thread(target=invoke, name="starship-visual-assessment", daemon=True)
        self._thread.start()

    def _command(self, action, row, *, origin, reason):
        self._awaiting_applied = True
        return {"schema": "missionos.starship_visual_dispatch.v1", "episode_id": self.episode["episode_id"],
                "event_id": self.episode["event"]["event_id"], "action": action, "origin": origin,
                "reason": reason, "time_s": row["time_s"], "deadline_s": self.episode["deadline_s"],
                "wall_elapsed_s": max(0., time.monotonic()-self._pace_origin[1]),
                "state_generation": self.generation, "authority_sha256": self.authority_sha256,
                "request_id": self.pending["request_id"] if self.pending else None}

    def step(self, row):
        now = row["time_s"]
        self._last_row = deepcopy(row)
        for command in self.commands:
            if command["later_observation"] is None and now > command["time_s"]:
                command["later_observation"] = deepcopy(row)
        if self._awaiting_applied:
            return None
        if self._closed_reason is not None:
            attempt = self.channel.current_attempt
            if (not self._budget_stop_applied and attempt is not None
                    and attempt["release_attempt_id"] != self.episode["event"]["release_attempt_id"]
                    and now >= attempt["source_time_s"]+self.config["confirmation_timeout_s"]
                    and not self._fresh(now, increment=True)):
                self._budget_stop_applied = True
                return self._command("stop_deployment", row, origin="rules", reason="visual_episode_budget_exhausted")
            return None
        if self.episode is None:
            deadline = min(now+self.config["hold_s"], row["return_deadline_s"], row.get("hold_expires_at_s") or float("inf"))
            event = self.detector.poll(now, self.channel.current_attempt, self.channel.latest,
                                      generation=self.generation, episode_deadline_s=deadline)
            if event is None:
                return None
            self.episode = {"episode_id": event["episode_id"], "event": event, "opened_at_s": now,
                            "deadline_s": deadline, "wall_budget_s": deadline-now,
                            "closed_at_s": None, "status": "open", "evidence_status": "pending"}
            self._pace_origin = (now, time.monotonic())
            if self.director is not None:
                self.director.supersede_for_visual(now)
            if row["sequencer_state"] == "running":
                return self._command("hold", row, origin="rules", reason="numeric_anomaly_precautionary_hold")
        wall_expired = time.monotonic()-self._pace_origin[1] >= self.episode["wall_budget_s"]
        if now >= self.episode["deadline_s"] or wall_expired:
            reason = "original_episode_deadline" if now >= self.episode["deadline_s"] else "original_episode_wall_deadline"
            command = self._command("stop_deployment", row, origin="fallback", reason=reason)
            if self.pending is not None:
                self.results.append({"request_id": self.pending["request_id"], "response": None,
                                     "received_at_s": now, "accepted": False, "rejection": reason})
            self.pending, self._answer = None, None
            if self.observation_requests and self.observation_requests[-1]["status"] == "pending":
                self.observation_requests[-1]["status"] = "expired"
                self.observation_results.append({"schema": "missionos.starship_visual_observation_result.v1",
                    "episode_id": self.episode["episode_id"],
                    "requested_at_s": self.observation_requests[-1]["requested_at_s"],
                    "completed_at_s": now, "evidence_kind": self._next_kind, "status": "expired"})
            return command
        if self.pending is not None:
            if self._answer is None or now <= self.pending["snapshot_time_s"]:
                return None
            request, answer = self.pending, deepcopy(self._answer)
            valid = (type(answer) is dict and answer.get("schema") == "missionos.starship_visual_result.v1"
                and answer.get("request_id") == request["request_id"] and answer.get("episode_id") == request["episode_id"]
                and answer.get("run_id") == self.run_id and answer.get("request_sha256") == digest(request)
                and answer.get("status") == "fixture_valid" and answer.get("model_inference_invoked") is False
                and answer.get("action") in request["allowed_actions"])
            receipt, diagnosis = answer.get("provider_receipt"), answer.get("visual_diagnosis")
            valid = valid and (type(receipt) is dict
                and receipt.get("status") == "fixture_succeeded" and receipt.get("transport_mode") == "loopback_fixture"
                and receipt.get("local_http_attempted") is True
                and all(receipt.get(key) is False for key in ("hosted_model_attempted", "model_inference_invoked", "call_attempted"))
                and receipt.get("semantic_request_sha256") == digest(request) and receipt.get("retries") == 0
                and type(diagnosis) is dict and diagnosis.get("request_sha256") == digest(request)
                and diagnosis.get("manifest_sha256") == digest(request["manifest"])
                and diagnosis.get("diagnosis_id") == digest({k:v for k,v in diagnosis.items() if k != "diagnosis_id"})
                and diagnosis.get("recommended_action") == answer.get("action")
                and diagnosis.get("observation_choice") == answer.get("observation_choice")
                and diagnosis.get("model_id") == self.authority["visual_model"])
            action, choice = answer.get("action"), answer.get("observation_choice")
            if valid:
                valid = (choice in request["observation_choices"] and (
                    action == "request_visual_observation" and choice in ("alternate_camera", "next_frame_primary")
                    or action == "request_numeric_confirmation" and choice == "fresh_separation_counter"
                    or action in ("continue", "stop_deployment") and choice == "none"))
            reason = None if valid else "invalid_visual_response"
            if valid:
                if (now+self.config["apply_margin_s"] >= self.episode["deadline_s"]
                        or self.remaining_s() <= self.config["apply_margin_s"]):
                    reason = "insufficient_apply_margin"
                elif request["state_generation"] != self.generation:
                    reason = "superseded_generation"
                elif action == "continue" and "continue" not in self._allowed(row, followup=True)[0]:
                    reason = "current_release_conditions_not_met"
            result = {"request_id": request["request_id"], "response": answer, "received_at_s": now,
                      "received_wall_elapsed_s": max(0., time.monotonic()-self._pace_origin[1]),
                      "current_state_generation": self.generation,
                      "accepted": reason is None, "rejection": reason}
            self.results.append(result)
            if reason:
                self.pending, self._answer = None, None
                self.episode["status"] = "waiting_fallback"
                return None
            if action in ("continue", "stop_deployment"):
                command = self._command(action, row, origin="fixture_judgment", reason="accepted_visual_response")
                self.pending, self._answer = None, None
                return command
            self._prior_diagnosis = deepcopy(answer.get("visual_diagnosis"))
            if self._prior_diagnosis is None:
                self.pending, self._answer = None, None
                self.episode["status"] = "waiting_fallback"
                return None
            if "diagnosis_id" not in self._prior_diagnosis:
                self._prior_diagnosis["diagnosis_id"] = digest(answer)
            operation = {"schema": "missionos.starship_visual_observation_request.v1",
                         "episode_id": request["episode_id"], "request_id": request["request_id"],
                         "choice": choice, "requested_at_s": now, "deadline_s": self.episode["deadline_s"],
                         "status": "pending"}
            self.observation_requests.append(operation)
            if action == "request_visual_observation" and self.collector is not None:
                operation["collector_receipt"] = self.collector.request_observation(choice, now)
                self._next_kind = "visual"
            else:
                self._next_kind = "numeric_followup"
            self._observation_due = now
            self.pending, self._answer = None, None
            return None
        if self.episode["status"] == "waiting_fallback":
            return None
        if self._observation_due is not None:
            requested = self._observation_due
            ready = (self._fresh(now) and self.channel.latest["source_time_s"] > requested
                     if self._next_kind == "numeric_followup" else bool(self._manifest(now).get("frames")))
            # The final judgment must consume currently arrived confirmation,
            # not spend its last slot just before an already pending counter
            # sample arrives. This is passive waiting under the original
            # deadline, not another sensor request or an extended hold.
            if self._next_kind == "visual" and row["sequencer_state"] == "held" and not self._fresh(now, increment=True):
                ready = False
            if not ready:
                return None
            result = {"schema": "missionos.starship_visual_observation_result.v1",
                      "episode_id": self.episode["episode_id"], "requested_at_s": requested,
                      "completed_at_s": now, "evidence_kind": self._next_kind, "status": "available"}
            self.observation_results.append(result)
            self.observation_requests[-1]["status"] = "completed"
            self._observation_due = None
        self._issue(row)
        return None

    def record_applied(self, command, row):
        if not self._awaiting_applied or command["authority_sha256"] != self.authority_sha256:
            raise ValueError("unbound_visual_dispatch")
        record = deepcopy(command)
        record.update(observation=deepcopy(row), later_observation=None)
        self.commands.append(record)
        self._last_command_time_s, self._awaiting_applied = row["time_s"], False
        if command["action"] in ("continue", "stop_deployment"):
            self._closed_reason = command["reason"]
            self.episode.update(status="closed", closed_at_s=row["time_s"], closure_action=command["action"],
                                closure_origin=command["origin"])

    def require_inventory_ready(self, now, physical_count):
        if self.episode is not None and self._closed_reason is None:
            raise ValueError("visual_episode_unclosed")
        if self.pending is not None or self._awaiting_applied:
            raise ValueError("visual_decision_pending")
        if not self._fresh(now) or self.observed_count != physical_count:
            raise ValueError("unresolved_inventory")
        if self._last_command_time_s is not None and self.channel.latest["source_time_s"] <= self._last_command_time_s:
            raise ValueError("inventory_does_not_cover_final_visual_action")
        if self._last_row is None or self._last_row["sequencer_state"] == "held":
            raise ValueError("visual_deployment_not_closed")
        return deepcopy(self.channel.latest)

    def finish(self):
        return {"schema": "missionos.starship_visual_record.v1", "run_id": self.run_id,
                "authority": deepcopy(self.authority), "authority_sha256": self.authority_sha256,
                "episode": deepcopy(self.episode), "events": deepcopy(self.detector.events),
                "attempts": deepcopy(self.channel.attempts), "counter_receipts": deepcopy(self.channel.receipts),
                "counter_rejections": deepcopy(self.channel.rejections), "observed_count": self.observed_count,
                "requests": deepcopy(self.requests), "results": deepcopy(self.results), "commands": deepcopy(self.commands),
                "observation_requests": deepcopy(self.observation_requests), "observation_results": deepcopy(self.observation_results),
                "superseded": deepcopy(self.superseded), "waiting_release_gates": deepcopy(self.waiting_release_gates),
                "hosted_model_attempted": False, "model_inference_invoked": False,
                "physical_execution": False, "automatic_retry": False}


class VisualDeploymentDirector(ObservedDeploymentDirector):
    def __init__(self, envelope, tracker, *, visual_runtime, **kwargs):
        super().__init__(envelope, tracker, **kwargs)
        self.visual_runtime = visual_runtime
        visual_runtime.director = self

    def pace(self, clock_id, time_s):
        super().pace(clock_id, time_s)
        if clock_id == "ship":
            self.visual_runtime.pace(time_s)

    def update(self, point, row, **kwargs):
        # Visual runs retain the legacy start/booster decision. The event actor
        # exclusively owns monitoring, so a healthy first release does not
        # consume the one anomaly episode or duplicate a visual operation.
        if point in ("deployment_monitor", "deployment_diagnostic", "deployment_reassessment"):
            self.confirm("ship", row)
            return None
        return super().update(point, row, **kwargs)

    def supersede_for_visual(self, now):
        request = self.pending.pop("ship", None)
        if request is not None:
            self.visual_runtime.superseded.append({"request_id": request["request_id"],
                "request_sha256": digest(request), "superseded_at_s": now})
            self.finished.add(request["point"])

    def visual_command(self, command, row):
        self.tracker.command(command["action"], row["time_s"])
        self.trace.append({"kind": "visual_command", "action": command["action"],
                           "time_s": row["time_s"], "episode_id": command["episode_id"], "origin": command["origin"]})
        self.visual_runtime.record_applied(command, row)

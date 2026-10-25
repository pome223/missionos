"""Worker-only restored return coordination; no credentials or model SDK.

The outer registered worker owns one explicit integration-policy scope across
prefix and continuation. A typed origin is not proof of that numerical policy;
only the outer completed scope receipt can establish it. Forecast children use
the same explicit policy independently. This coordinator never imports a host
agent, creates a ledger, assigns predicted state or grants qualification.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import secrets
import time

from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, MAX_FRAME_BYTES
from .starship_restored_admission import (
    RestoredAdmissionPolicy, RestoredCandidate, RestoredCurrentObservation,
    RestoredOperationalNotice, RestoredResourceState, RestoredSitePolicy,
    admit_restored_candidate,
)
from .starship_restored_approval import RestoredApprovalBinding, RestoredApprovalWorker
from .starship_restored_approval_admission import RestoredApprovalAdmissionBridge
from .starship_restored_contract import ActualRestoredOrigin, RestoredCandidateSchedule, _manifest
from .starship_restored_decision_pipe import RestoredDecisionClient
from .starship_restored_forecast_pool import RestoredForecastBatch, _save, _save_bytes, _wire
from .starship_restored_integration import RestoredIntegrationPolicy
from .starship_restored_return import RestoredReturnExecution, RestoredReturnRequest
from .starship_restored_return_context import SOURCE as RETURN_CONTEXT_SOURCE, projection_facts
from .starship_restored_wait import RestoredPendingPacer, RestoredWaitEpoch

SCHEMA = "missionos.starship_restored_return_governance.v1"
READY = "missionos.starship_restored_origin_ready.v1"
ACK = "missionos.starship_restored_origin_ready_ack.v1"
TOOL_WINDOW_S = 300.
JUDGMENT_WINDOW_S = 180.


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _hash(value):
    return sha256(_wire(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class RestoredReturnCoordinator:
    """One actual continuation, finite registered notice events and one amendment.

    Construction performs only typed validation. ``start`` writes the exclusive
    origin-ready handoff; ``tick`` pumps channels, paces pending work and advances
    at most one existing finite interval. It must run inside the outer worker's
    explicit policy2 scope, never in the host/Gateway process. ``notice_resources``
    is a trusted registered tool factory over detached actual snapshots.
    """

    def __init__(self, closed_origin, *, profile_raw_bytes, expected_source_sha256,
                 integration_policy, context, plan_sha256, output_dir, decision_client,
                 ready_writer, ready_reader, approval_request_writer, approval_grant_reader,
                 telemetry_writer, sites, notice_resources, notice_generations,
                 deployment_record, notice_summary=lambda _notice: "",
                 clock=time.monotonic, sleep=time.sleep, return_context_projection=False,
                 return_review_policy=None, final_handoff_policy=None):
        _require(type(closed_origin) is ActualRestoredOrigin
            and type(integration_policy) is RestoredIntegrationPolicy
            and integration_policy.subdivisions == 2
            and type(decision_client) is RestoredDecisionClient,
            "restored_coordinator_exact_origin_policy2_and_client_required")
        _manifest(expected_source_sha256)
        if final_handoff_policy is not None:
            from .starship_final_handoff import FinalActualReturnHandoffPolicy
            _require(type(final_handoff_policy) is FinalActualReturnHandoffPolicy,
                "restored_coordinator_exact_final_handoff_policy_required")
            final_handoff_policy.validate_binding(expected_source_sha256, plan_sha256)
        self._final_handoff_policy, self._final_handoff = final_handoff_policy, None
        _require(type(return_context_projection) is bool and (not return_context_projection
            or RETURN_CONTEXT_SOURCE in expected_source_sha256), "restored_coordinator_return_context_opt_in_or_source")
        closed_origin.validate_profile(profile_raw_bytes)
        _require(_wire(expected_source_sha256) == _wire(closed_origin.record()["source_sha256"])
            and "src/runtime/starship_restored_coordinator.py" in expected_source_sha256,
            "restored_coordinator_source_binding_changed")
        _require(all(isinstance(x, ApprovalPipeWriter) for x in
            (ready_writer, approval_request_writer, telemetry_writer))
            and all(isinstance(x, ApprovalPipeReader) for x in (ready_reader, approval_grant_reader)),
            "restored_coordinator_inherited_pipes_required")
        channels = (decision_client.writer, decision_client.reader, ready_writer, ready_reader,
            approval_request_writer, approval_grant_reader, telemetry_writer)
        _require(len({(os.fstat(channel.fd).st_dev, os.fstat(channel.fd).st_ino) for channel in channels}) == len(channels),
            "restored_coordinator_channels_must_be_distinct")
        _require(type(sites) is tuple and sites and all(type(x) is RestoredSitePolicy for x in sites)
            and type(notice_generations) is tuple and notice_generations
            and all(type(x) is int and x >= 0 for x in notice_generations)
            and tuple(sorted(set(notice_generations))) == notice_generations
            and all(callable(x) for x in (notice_resources, notice_summary, clock, sleep))
            and type(deployment_record) is dict
            and deployment_record.get("schema") == "missionos.starship_restored_deployment_judgments.v1",
            "restored_coordinator_registered_tools_or_notice_events_required")
        # Reuse binding validation without creating a host ledger or advancing.
        RestoredApprovalBinding(closed_origin, context=context, plan_sha256=plan_sha256,
            expected_source_sha256=expected_source_sha256, started_wall_s=0.)
        decision_binding = {"context": context, "plan_sha256": plan_sha256,
            "source_sha256": _hash(expected_source_sha256)}
        if return_review_policy is not None:
            from .starship_restored_return_review import validate_policy, policy_digest
            return_review_policy = validate_policy(return_review_policy)
            _require(expected_source_sha256.get("src/runtime/starship_restored_return_review.py")
                == sha256(Path(__file__).with_name("starship_restored_return_review.py").read_bytes()).hexdigest(),
                "restored_coordinator_return_review_source_opt_in_required")
            decision_binding["return_review_policy_sha256"] = policy_digest(return_review_policy)
        _require(decision_client.binding == decision_binding
            and _wire(getattr(decision_client, "return_review_policy", None)) == _wire(return_review_policy),
            "restored_coordinator_decision_pipe_binding_changed")
        self.origin, self.profile = closed_origin, profile_raw_bytes
        self.source = json.loads(_wire(expected_source_sha256))
        self.policy, self.context, self.plan = integration_policy, deepcopy(context), plan_sha256
        self.output = Path(output_dir).absolute()
        self.client = decision_client
        self.ready_writer, self.ready_reader = ready_writer, ready_reader
        self.approval_writer, self.approval_reader, self.telemetry = approval_request_writer, approval_grant_reader, telemetry_writer
        self.sites, self.tools, self.generations, self.summary = sites, notice_resources, notice_generations, notice_summary
        self.deployment = deepcopy(deployment_record)
        self.return_context_projection = return_context_projection
        self.clock, self.sleep = clock, sleep
        self.anchor = RestoredCandidateSchedule(closed_origin)
        self.requests = {candidate: RestoredReturnRequest(closed_origin, self.anchor, candidate,
            profile_raw_bytes, self.source) for candidate in ("nominal", "next_orbit")}
        self.initial_sha = _hash(self.requests["nominal"].record())
        self.stage, self.execution, self.binding, self.actor, self.bridge = "created", None, None, None, None
        self.batch, self.predictions, self.ready, self.ack = None, None, None, None
        self.pending, self.used_generations, self.notice_hashes = None, set(), {}
        self.last_generation, self.latest_tools = -1, None
        self.decisions, self.changes, self.batches, self.events = [], [], [], []
        self.dispatch = None
        self._telemetry_key, self._telemetry_sequence = None, 0
        self._observed_key, self._observed_wall = None, None
        self._decision, self._last_candidate = None, None

    def _ref(self, path, *, packed=False):
        raw = path.read_bytes()
        row = {"path": path.relative_to(self.output).as_posix(), "sha256": sha256(raw).hexdigest()}
        if packed:
            from .starship_restored_forecast_pool import _restore
            row["raw_sha256"] = sha256(_restore(raw)).hexdigest()
        return row

    def start(self):
        _require(self.stage == "created", "restored_coordinator_single_use")
        _require(not self.output.exists() and not self.output.is_symlink(), "restored_coordinator_exclusive_output_required")
        self.output.mkdir(parents=True, mode=0o700)
        _save_bytes(self.output/"closed-origin.json", self.origin.source_record_bytes())
        self.execution = RestoredReturnExecution(self.requests["nominal"],
            origin_record_bytes=self.origin.source_record_bytes(),
            **({"final_handoff_policy": self._final_handoff_policy} if self._final_handoff_policy is not None else {}))
        wall = self.clock()
        self.binding = RestoredApprovalBinding(self.origin, context=self.context, plan_sha256=self.plan,
            expected_source_sha256=self.source, started_wall_s=wall)
        bound = self.binding.record()
        self.ready = {"schema": READY, "context": self.context, "plan_sha256": self.plan,
            "source_sha256": _hash(self.source), "profile_sha256": self.origin.record()["profile_sha256"],
            "origin_file": "closed-origin.json", "source_record_sha256": self.origin.source_record_sha256,
            "origin_sha256": _hash(self.origin.record()), "binding_sha256": _hash(bound),
            "opportunity_anchor_sha256": _hash(self.anchor.opportunity_anchor_record()),
            "initial_reservation_sha256": self.initial_sha, "integration_policy_sha256": _hash(self.policy.record()),
            "started_wall_s": wall, "nonce": secrets.token_hex(32)}
        _require(len(_wire(self.ready)) <= MAX_FRAME_BYTES, "restored_coordinator_ready_frame_too_large")
        _save(self.output/"origin-ready.json", self.ready)
        self.ready_writer.send(self.ready)
        self.stage = "await_host_ready"
        # Handshake time consumes the original tool epoch; ACK never resets it.
        end = min(self.origin.time_s+TOOL_WINDOW_S,
            bound["bounds"]["simulation_deadline_s"]-JUDGMENT_WINDOW_S,
            bound["bounds"]["reservation_cutoff_s"]-JUDGMENT_WINDOW_S)
        _require(end > self.origin.time_s, "restored_coordinator_no_tool_and_judgment_window")
        self.tool_epoch = RestoredWaitEpoch(self.origin.time_s, wall, end,
            wall+end-self.origin.time_s, self._maximum_step())
        self.pending = RestoredPendingPacer(self.tool_epoch, clock=self.clock, sleep=self.sleep)
        self._emit()
        return deepcopy(self.ready)

    def _maximum_step(self):
        profile = self.requests["nominal"].profile()["integration"]
        return max(profile["powered_dt_s"], profile["coast_dt_s"])

    def _snapshot(self):
        return self.execution.snapshot()

    def _state(self):
        snapshot = self._snapshot()
        key = _hash(snapshot["state"])
        if key != self._observed_key:
            self._observed_key, self._observed_wall = key, self.clock()
        return self._runtime_state(snapshot, self._observed_wall)

    def _runtime_state(self, snapshot, observed_wall):
        bound = self.binding.record()
        revision, scope = (0, bound["initial_scope"]) if self.actor is None else (self.actor.revision, self.actor.scope)
        return {"run_active": not snapshot["completed"], "simulation_time_s": snapshot["state"]["time_s"],
            "restored_origin_sha256": bound["restored_origin_sha256"], "revision": revision,
            "scope": deepcopy(scope), "reservation_sha256": _hash(snapshot["binding"]),
            "observation": {"state_sha256": _hash(snapshot["state"]), "simulation_time_s": snapshot["state"]["time_s"],
                "wall_time_s": observed_wall}}

    def _emit(self):
        state, snapshot = self._state(), self._snapshot()
        return self._send_telemetry(state, snapshot)

    def _send_telemetry(self, state, snapshot, *, force=False):
        key = (state["simulation_time_s"], state["revision"], state["reservation_sha256"])
        if self.telemetry.flush() and (force or key != self._telemetry_key):
            self._telemetry_sequence += 1
            frame = {"schema": "missionos.starship_restored_worker_telemetry.v1", "context": self.context,
                "plan_sha256": self.plan, "source_sha256": _hash(self.source), "sequence": self._telemetry_sequence,
                "runtime_state": state, "actual_state": snapshot["state"], "phase": snapshot["phase"],
                "vehicle_sha256": _hash(snapshot["vehicle"]), "binding_sha256": _hash(snapshot["binding"]),
                "return_controller_started": snapshot["return_controller"] is not None,
                "integration_policy_sha256": _hash(self.policy.record()), "state_assigned": False}
            _require(len(_wire(frame)) <= MAX_FRAME_BYTES, "restored_coordinator_telemetry_frame_too_large")
            self.telemetry.send(frame)
            self._telemetry_key = key
            return self._telemetry_sequence
        return None

    def _reacquire_amendment_observation(self, expected_snapshot, expected_tools):
        """Re-read the actual state owner after expensive saved-data admission.

        Same values may be observed again at the same held plant tick. They
        must come from the actual generator provider and exactly match every
        state, vehicle, phase and reservation field used by admission. Changed
        data fail closed rather than silently borrowing the previous result.
        """
        snapshot, receipt = self.execution.reacquire_actual_snapshot(clock=self.clock)
        _require(_wire(snapshot) == _wire(expected_snapshot)
            and snapshot["phase"] == "orbital_coast" and snapshot["return_controller"] is None,
            "restored_coordinator_actual_state_changed_after_admission")
        tools = self._context()
        _require(all(_wire(current.record()) == _wire(previous.record())
            for current, previous in zip(tools, expected_tools, strict=True)),
            "restored_coordinator_actual_tools_changed_after_admission")
        state = self._runtime_state(snapshot, receipt["wall_time_s"])
        _require(receipt["state_sha256"] == state["observation"]["state_sha256"]
            and receipt["binding_sha256"] == state["reservation_sha256"]
            and 0 <= self.clock()-receipt["wall_time_s"] <= self.binding.record()["bounds"]["maximum_observation_age_s"],
            "restored_coordinator_reacquired_observation_not_fresh")
        sequence = self._send_telemetry(state, snapshot, force=True)
        _require(sequence is not None and self.telemetry.flush(),
            "restored_coordinator_approval_observation_not_delivered")
        # Update only from this completed actual-read receipt. This is neither
        # clearing a stale cache nor restamping an earlier observation.
        self._observed_key, self._observed_wall = receipt["state_sha256"], receipt["wall_time_s"]
        return state, receipt | {"telemetry_sequence": sequence}

    def _context(self):
        values = self.tools(deepcopy(self._snapshot()))
        _require(type(values) is tuple and len(values) == 2
            and type(values[0]) is RestoredOperationalNotice and type(values[1]) is RestoredResourceState,
            "restored_coordinator_exact_notice_and_resource_required")
        notice, _resources = values
        _require(notice.generation in self.generations and notice.generation >= self.last_generation,
            "restored_coordinator_unregistered_or_replayed_notice")
        digest = _hash(notice.record())
        _require(self.notice_hashes.get(notice.generation, digest) == digest,
            "restored_coordinator_notice_content_changed_without_generation")
        self.notice_hashes[notice.generation], self.last_generation = digest, notice.generation
        self.latest_tools = values
        return values

    def _candidate(self, candidate_id):
        _require(self.predictions is not None, "restored_coordinator_forecast_unavailable")
        notice, resources = self._context()
        result = self.predictions[0 if candidate_id == "nominal" else 1]
        return RestoredCandidate(result, self.origin, self.anchor, self.profile, self.source,
            RestoredAdmissionPolicy(), self.sites, notice, resources)

    def _admit(self, candidate_id, stage):
        candidate = self._candidate(candidate_id)
        notice, resources = self.latest_tools
        observation = RestoredCurrentObservation(self.origin, self.execution)
        return candidate, admit_restored_candidate(candidate, observation, notice=notice, resources=resources, stage=stage)

    def _bridge_inputs(self, action, _state):
        candidate = self._candidate("next_orbit" if action == "select_next_orbit" else "nominal")
        self._bridge_candidate = candidate
        notice, resources = self.latest_tools
        return candidate, RestoredCurrentObservation(self.origin, self.execution), notice, resources

    def _command(self, action, state, consumption):
        _require(action == "select_next_orbit" and not self.changes
            and state["simulation_time_s"] < self.anchor.record()["nominal_s"],
            "restored_coordinator_one_pre_nominal_reservation_change_only")
        admission = self.bridge(action, state)
        _require(admission["accepted"] is True, "restored_coordinator_amendment_candidate_not_admitted")
        candidate = self._bridge_candidate.record()
        _require(candidate["candidate_sha256"] == admission["current_candidate_sha256"],
            "restored_coordinator_amendment_fresh_proof_changed")
        actual_snapshot = self._snapshot()
        origin = self.execution.current_origin()
        path = self.output/"reservation-change-origin.json"
        _save_bytes(path, origin.source_record_bytes())
        schedule = RestoredCandidateSchedule(origin, anchor=self.anchor)
        request = RestoredReturnRequest(origin, schedule, "next_orbit", self.profile, self.source)
        self.execution.replace_reservation(request)
        change = self.execution._context["reservation_changes"][-1]
        self.changes.append({"origin": self._ref(path), "reservation_change_sha256": _hash(change),
            "actual_snapshot": actual_snapshot, "admission": admission, "confirmation_snapshot": None})
        self.changes[-1]["candidate"] = candidate
        return {"applied": True, "revision": consumption["revision"], "reservation_sha256": _hash(request.record())}

    def _begin_forecasts(self):
        self.batch = RestoredForecastBatch(tuple(self.requests.values()), output_dir=self.output/"forecasts",
            wall_deadline_monotonic=self.tool_epoch.wall_deadline_s,
            expected_source_sha256=self.source, integration_policy=self.policy)
        self.batch.start()
        self.stage = "forecasting"

    def _batch_record(self):
        root = self.batch.output_dir
        job = json.loads((root/"job.json").read_bytes())
        return {"job": self._ref(root/"job.json"), "batch_claim": self._ref(root/"batch.claim"),
            "profile": self._ref(root/job["profile_file"]),
            "origins": {name: self._ref(root/name) for name in job["input_sha256"] if name != job["profile_file"]},
            "candidates": {candidate: {"prediction": self._ref(root/candidate/"prediction.json.gz", packed=True),
                "result": self._ref(root/candidate/"result.json"), "integration": self._ref(root/candidate/"integration.json"),
                "started": self._ref(root/candidate/"started.json"), "worker_claim": self._ref(root/candidate/"worker.claim")}
                for candidate in self.requests}}

    def _begin_judgment(self):
        notice, resources = self._context()
        if notice.generation in self.used_generations:
            return
        now, wall, bound = self._state()["simulation_time_s"], self.clock(), self.binding.record()
        end = min(now+JUDGMENT_WINDOW_S, bound["bounds"]["simulation_deadline_s"], bound["bounds"]["reservation_cutoff_s"])
        wall_end = min(wall+end-now, bound["bounds"]["wall_deadline_s"])
        end = min(end, now+max(0., wall_end-wall))
        if end-now < 35. or wall_end <= wall:
            return
        candidates = {key: self._admit(key, "selection") for key in self.requests}
        _require(all(candidate.record()["notice_sha256"] == _hash(notice.record())
            and candidate.record()["resource_sha256"] == _hash(resources.record())
            for candidate, _check in candidates.values()), "restored_coordinator_decision_context_changed_during_tools")
        facts = {"time_s": now, "released_count": self.origin.record()["released_count"],
            "retained_count": self.origin.record()["retained_count"], "sequence_closed": True,
            "authority.revision": self._state()["revision"], "authority.nominal_allowed": True,
            "authority.next_orbit_allowed": self._state()["revision"] == 1,
            "authority.can_request_amendment": self.actor.status == "idle",
            "authority.approval_deadline_s": bound["bounds"]["simulation_deadline_s"],
            "notice.generation": notice.generation, "notice.issued_time_s": notice.issued_s,
            "notice.expiry_time_s": notice.expires_s, "resources.power_kwh": resources.energy_kwh,
            "resources.temperature_k": resources.temperature_k,
            "resources.propellant_loss_kg": resources.modeled_propellant_loss_kg}
        for key, (candidate, check) in candidates.items():
            row, contact = candidate.record(), candidate.record()["numerical_contact"]
            account = check["resource_accounting"]
            facts.update({f"candidate.{key}.return_time_s": row["proposal"]["return_time_s"],
                f"candidate.{key}.additional_wait_s": row["proposal"]["return_time_s"]-self.anchor.record()["nominal_s"],
                f"candidate.{key}.power_kwh": account["energy_at_return_kwh"],
                f"candidate.{key}.temperature_k": account["temperature_at_return_k"],
                f"candidate.{key}.propellant_loss_kg": account["modeled_loss_budget_kg"],
                f"candidate.{key}.feasibility_passed": check["accepted"]})
            for source, dest in (("event_time_s", "arrival_time_s"), ("point_speed_mps", "contact_speed_mps"),
                    ("tilt_deg", "tilt_deg"), ("propellant_kg", "remaining_propellant_kg"),
                    ("latitude_deg", "latitude_deg"), ("longitude_deg", "longitude_deg")):
                if source in contact:
                    facts[f"candidate.{key}.{dest}"] = contact[source]
        if getattr(self, "return_context_projection", False):
            facts.update(projection_facts(self._snapshot()["binding"],
                {key: (candidate.record(), check) for key, (candidate, check) in candidates.items()}, observed_s=now))
        actions = ["select_nominal", "keep_reserved_return", "select_next_orbit", "request_scope_amendment", "wait", "request_deep_reasoning"]
        if self.changes:
            actions = ["keep_reserved_return", "wait", "request_deep_reasoning"]
        request = {"schema": "missionos.starship_restored_decision_request.v1",
            "request_id": f"restored-return-notice-{notice.generation}", "phase": "return",
            "scope_sha256": _hash(self._state()["scope"]), "time_s": now, "deadline_s": end,
            "facts": facts, "notice_summary": self.summary(notice), "allowed_actions": actions}
        # Independent admission can be expensive. It consumes this original
        # judgment epoch rather than granting a new provider window. Refuse
        # before writing a request when a full initial Jev budget is gone.
        if wall+end-now-self.clock() < 35.:
            self._stop("judgment_window_unavailable")
            return
        frame = self.client.begin(request, started_wall_s=wall)
        # Client and pacer share the same original request epoch; no new clock.
        self.pending = RestoredPendingPacer(RestoredWaitEpoch(now, frame["started_wall_s"], end,
            frame["wall_deadline_s"], self._maximum_step()), clock=self.clock, sleep=self.sleep)
        self._decision = {"request": deepcopy(request), "generation": notice.generation,
            "request_snapshot": self._snapshot(),
            "request_candidates": {key: candidate.record() for key, (candidate, _check) in candidates.items()}}
        self.used_generations.add(notice.generation)
        self.stage = "judging"

    def _choice(self, exchange):
        action = exchange.get("action") if exchange.get("status") == "valid" else None
        record = {"request_id": self._decision["request"]["request_id"], "original_action": action,
            "disposition": None, "actual_snapshot": self._snapshot(), "candidate": None, "admission": None,
            "request_snapshot": deepcopy(self._decision["request_snapshot"]),
            "request_candidates": deepcopy(self._decision["request_candidates"])}
        self.decisions.append(record)
        if action is None:
            candidate, check = self._admit("nominal", "selection")
            record.update(candidate=candidate.record(), admission=check,
                disposition="unavailable_preserved_preapproved_nominal" if check["accepted"] and not self.changes else "unavailable_not_admitted")
            if not check["accepted"] or self.changes:
                return self._stop("model_no_valid_judgment")
        elif action == "wait":
            record["disposition"] = "valid_wait_reservation_unchanged"
        else:
            candidate_id = "next_orbit" if action in ("select_next_orbit", "request_scope_amendment") else self._snapshot()["binding"]["candidate_id"]
            candidate, check = self._admit(candidate_id, "selection")
            record.update(candidate=candidate.record(), admission=check)
            if not check["accepted"]:
                record["disposition"] = "valid_choice_rejected_not_overwritten"
            elif candidate_id == "next_orbit" and not self.changes:
                _require(self.actor.status == "idle", "restored_coordinator_amendment_budget_exhausted")
                state, observation = self._reacquire_amendment_observation(record["actual_snapshot"], self.latest_tools)
                record["actual_observation_reacquisition"] = observation
                self.actor.request_change(request_id="restored-return-amendment",
                    candidate_sha256=candidate.record()["proposal_sha256"], state=state)
                record["disposition"] = "scope_amendment_requested"
                bound, now, wall = self.binding.record(), self._state()["simulation_time_s"], self.clock()
                self.pending = RestoredPendingPacer(RestoredWaitEpoch(now, wall,
                    bound["bounds"]["simulation_deadline_s"], bound["bounds"]["wall_deadline_s"], self._maximum_step()),
                    clock=self.clock, sleep=self.sleep)
                self.stage = "awaiting_operator"
            else:
                record["disposition"] = "valid_keep_reservation_unchanged"
            if self.stage == "awaiting_operator":
                return
        self.stage, self.pending = "waiting_reserved", None

    def _stop(self, reason, candidate=None, admission=None):
        if self.batch is not None and self.batch.state in ("starting", "running"):
            self.batch.close()
        self.dispatch = {"actual_snapshot": self._snapshot(),
            "candidate": None if candidate is None else candidate.record(), "admission": admission,
            "gate_met": False, "refusal_reason": reason}
        self.execution.stop_before_dispatch(reason)
        self.stage, self.pending = "inhibited", None

    def _pump(self):
        self.ready_writer.flush()
        if self.stage == "await_host_ready":
            incoming = self.ready_reader.poll()
            if incoming is not None:
                expected = {"schema": ACK, "ready_sha256": _hash(self.ready), "nonce": self.ready["nonce"],
                    "context": self.context, "plan_sha256": self.plan, "source_sha256": _hash(self.source),
                    "binding_sha256": self.ready["binding_sha256"], "initial_reservation_sha256": self.initial_sha,
                    "ledger_ready": True}
                _require(_wire(incoming) == _wire(expected), "restored_coordinator_host_ack_binding_changed")
                self.ack = deepcopy(incoming)
                self._begin_forecasts()
        elif self.stage == "forecasting":
            results = self.batch.poll()
            if results is not None:
                self.predictions = results
                self.batches.append(self._batch_record())
                next_candidate, nominal = self._candidate("next_orbit"), self._candidate("nominal")
                self.bridge = RestoredApprovalAdmissionBridge(self.binding, next_candidate, nominal, current_inputs=self._bridge_inputs)
                self.actor = RestoredApprovalWorker(self.binding, initial_reservation_sha256=self.initial_sha,
                    request_writer=self.approval_writer, grant_reader=self.approval_reader,
                    admission=self.bridge, command=self._command, clock=self.clock)
                self.stage, self.pending = "waiting_reserved", None
                self._begin_judgment()
        elif self.stage == "judging":
            exchange = self.client.poll(self._state()["simulation_time_s"])
            if exchange is not None:
                self._choice(exchange)
        elif self.stage == "awaiting_operator":
            result = self.actor.tick(self._state())
            if result is not None:
                if self.actor.status == "reservation_changed_waiting_observation":
                    self.stage, self.pending = "confirming_reservation", None
                elif self.actor.status == "fallback_preserved":
                    self.stage, self.pending = "waiting_reserved", None
                else:
                    self._stop("approval_unavailable")

    def tick(self):
        _require(self._final_handoff is None, "restored_coordinator_permanently_handed_off")
        _require(self.stage != "created", "restored_coordinator_not_started")
        if self.stage in ("completed", "inhibited"):
            return self._snapshot()
        try:
            snapshot = self._snapshot()
            if snapshot["completed"]:
                self.stage = "completed"
                return snapshot
            self._emit()
            if snapshot["return_controller"] is None:
                self._context()
            if self.pending is not None:
                before = self.stage
                paced = self.pending.at_actual_tick(snapshot["state"]["time_s"], pump=self._pump)
                if paced["pending_deadline_expired"] and self.stage == before:
                    if before in ("await_host_ready", "forecasting"):
                        self._stop("host_ready_unavailable" if before == "await_host_ready" else "forecast_unavailable")
                    elif before == "judging":
                        self._choice({"status": "no_valid_judgment", "action": None})
                    else:
                        self.actor.tick(self._state())
                        self._stop("approval_unavailable")
            elif self.stage == "confirming_reservation":
                self.actor.confirm(self._state())
                if self.actor.status == "reservation_confirmed":
                    _require(self.changes and self.changes[-1]["confirmation_snapshot"] is None,
                        "restored_coordinator_missing_or_replayed_confirmation")
                    self.changes[-1]["confirmation_snapshot"] = self._snapshot()
                    self.stage = "waiting_reserved"
                elif self.actor.status != "reservation_changed_waiting_observation":
                    self._stop("reservation_confirmation_unavailable")
            elif self.stage == "waiting_reserved":
                self._begin_judgment()
            if self.stage == "inhibited":
                return self._snapshot()
            snapshot = self._snapshot()
            if snapshot["return_controller"] is None and snapshot["state"]["time_s"] >= snapshot["binding"]["return_time_s"]:
                if self.stage != "waiting_reserved" or self.actor is None or self.actor.status in (
                        "reservation_update_unconfirmed", "reservation_changed_waiting_observation", "reservation_confirmation_failed"):
                    self._stop("dispatch_not_admitted")
                    return self._snapshot()
                candidate, admission = self._admit(snapshot["binding"]["candidate_id"], "dispatch")
                self.dispatch = {"actual_snapshot": snapshot, "candidate": candidate.record(),
                    "admission": admission, "gate_met": admission["accepted"],
                    "refusal_reason": None if admission["accepted"] else "dispatch_not_admitted"}
                if not admission["accepted"]:
                    self._stop("dispatch_not_admitted", candidate, admission)
                    return self._snapshot()
                if self._final_handoff_policy is not None:
                    from .starship_final_handoff import _accepted_actual_dispatch
                    _require(self._telemetry_key == (snapshot["state"]["time_s"], self.actor.revision,
                            _hash(snapshot["binding"])) and self.telemetry.flush(),
                        "restored_coordinator_final_handoff_actual_telemetry_not_delivered")
                    self._final_handoff = self.execution.handoff_actual_return(
                        self._final_handoff_policy, dispatch=_accepted_actual_dispatch(self._final_handoff_policy,
                            candidate=candidate, admission=admission, snapshot=snapshot))
                    self.stage = "handed_off"
                    return self._snapshot()
                self.stage = "returning"
            self._emit()
            self.execution.step_once()
            if self.execution.result is not None:
                self.stage = "completed"
            return self._snapshot()
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
            self.events.append({"event": "coordinator_boundary_rejected", "stage": self.stage,
                "exception_type": type(error).__name__, "actual_time_s": self._snapshot()["state"]["time_s"]})
            if self._snapshot()["return_controller"] is not None or self.execution.result is not None:
                raise
            self._stop("source_or_observation_changed")
            return self._snapshot()

    @property
    def result(self):
        return None if self.execution is None else self.execution.result

    @property
    def final_handoff(self):
        return self._final_handoff

    def close(self):
        """Reap forecast peers on every outer exit; never advance actual state."""
        if self.batch is not None:
            self.batch.close()

    def record(self):
        _require(self.stage != "created", "restored_coordinator_not_started")
        record = {"schema": SCHEMA, "closed_origin": {"raw": self._ref(self.output/"closed-origin.json"),
                "origin_sha256": _hash(self.origin.record())},
            "opportunity_anchor": self.anchor.opportunity_anchor_record(),
            "initial_reservation": {"request": self.requests["nominal"].record(), "sha256": self.initial_sha},
            "integration_policy": self.policy.record(), "handshake": {"ready": deepcopy(self.ready), "ack": deepcopy(self.ack)},
            "forecast_batches": deepcopy(self.batches), "deployment": deepcopy(self.deployment),
            "decision_client": self.client.record(), "decisions": deepcopy(self.decisions),
            "approval": {"binding": self.binding.record(), "worker": None if self.actor is None else self.actor.record()},
            "current_origin_changes": deepcopy(self.changes), "dispatch": deepcopy(self.dispatch),
            "stage": self.stage, "notice_generations": list(self.generations),
            "events": deepcopy(self.events),
            "origin_proves_numerical_policy": False, "outer_completed_actual_integration_receipt_required": True,
            "qualification_refreshed": False, "mission_authority_granted": False, "physical_execution": False}
        if self._final_handoff is not None:
            record.update(schema="missionos.starship_final_pre_dispatch_governance.v1",
                final_handoff=self._final_handoff.record(),
                final_handoff_telemetry_sequence=self._telemetry_sequence, completed_v1_flight=False)
        elif self._final_handoff_policy is not None and self.stage == "inhibited":
            record.update(schema="missionos.starship_final_refused_governance.v1",
                final_refusal_owner=self.execution.final_refusal_owner_record(self._final_handoff_policy),
                completed_v1_flight=False)
        return record

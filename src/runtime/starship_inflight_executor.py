"""Opt-in return reservation authority; ordinary ReplanningExecutor is unchanged.

The inherited host channel authorizes one candidate-bound amendment. This module
holds no signing key/operator credential and does not claim HMAC verification.
"""
from copy import deepcopy
import time

from .starship_inflight_approval import InflightApprovalError, digest
from .starship_inflight_runtime import (WorkerApprovalActor, _context, _bounds, _hash, _number,
    amendment_window, APPROVAL_COAST_GUARD_S)
from .starship_orbital_window import reserved_decision_s
from .starship_replanning import notices
from .starship_replanning_executor import ReplanningExecutor

INITIAL_COMMANDS = ["select_nominal", "keep_reserved_return"]
FIXTURE_POLICY = {"schema": "missionos.inflight_fixture_policy.v1", "id": "request_checked_next_orbit_v1",
                  "model_inference_invoked": False, "maximum_scope_requests": 1}


def _require(value, reason):
    if not value:
        raise InflightApprovalError(reason)


def validate_inflight_executor_contract(contract, envelope, *, run_id, nominal_burn_s=None):
    """Pure preflight; call before any numerical prefix/model invocation.

    When the actual nominal burn is not yet known, structural checks still run.
    The executor repeats the exact cutoff check after its physical handoff.
    """
    required = {"schema", "context", "initial_scope", "bounds"}
    _require(type(contract) is dict and set(contract) in (required, required | {"fixture_policy"})
             and contract["schema"] == "missionos.inflight_executor_contract.v1", "invalid_inflight_executor_contract")
    if "fixture_policy" in contract:
        _require(envelope.get("mode") == "fixture" and digest(contract["fixture_policy"]) == digest(FIXTURE_POLICY),
                 "unregistered_inflight_fixture_policy")
    _context(contract["context"])
    _bounds(contract["bounds"])
    _require(contract["context"]["run_id"] == run_id, "inflight_run_id_mismatch")
    expected = {"commands": INITIAL_COMMANDS,
                "resource_limits": {"maximum_return_delay_s": envelope["maximum_delay_s"]}}
    _require(digest(contract["initial_scope"]) == digest(expected), "initial_return_scope_must_be_nominal_only")
    _require("reservation_cutoff_s" in contract["bounds"] and envelope.get("event_supervision"),
             "inflight_event_supervision_and_cutoff_required")
    _require(contract["bounds"]["simulation_deadline_s"] <= contract["bounds"]["reservation_cutoff_s"],
             "approval_must_protect_nominal_cutoff")
    if nominal_burn_s is not None:
        _require(contract["bounds"]["simulation_deadline_s"] < nominal_burn_s
                 and contract["bounds"]["reservation_cutoff_s"] == nominal_burn_s
                 - envelope["event_supervision"]["deadline_margin_s"], "approval_must_protect_nominal_cutoff")
    return deepcopy(contract)


class InflightReplanningExecutor(ReplanningExecutor):
    """One amendment; physical eligibility is visible but is never authority.

    ``snapshot_sink`` must forward trusted executor telemetry to the host using
    a separate bounded IPC path. Never create host observation history from the
    approval request itself. The runner owns that IPC and operator HTTP process.
    """

    def __init__(self, *args, inflight, request_writer, grant_reader, snapshot_sink, clock=time.monotonic, **kwargs):
        validate_inflight_executor_contract(inflight, args[1] if len(args) > 1 else kwargs["envelope"],
                                            run_id=kwargs.get("run_id", "standalone"))
        self._inflight_ready = False
        super().__init__(*args, **kwargs)
        self.connect_approval(inflight=inflight, request_writer=request_writer,
                              grant_reader=grant_reader, snapshot_sink=snapshot_sink, clock=clock)

    def connect_approval(self, *, inflight, request_writer, grant_reader, snapshot_sink, clock=time.monotonic):
        _require(not getattr(self, "_inflight_ready", False), "inflight_executor_already_connected")
        validate_inflight_executor_contract(inflight, self.envelope, run_id=self.run_id,
                                            nominal_burn_s=self.times["nominal"])
        scope = inflight["initial_scope"]
        _require(callable(snapshot_sink), "trusted_snapshot_sink_required")
        self.inflight_contract = deepcopy(inflight)
        self.active_scope, self.scope_revision = deepcopy(scope), 0
        self._approval_clock, self._snapshot_sink = clock, snapshot_sink
        self._authorized_candidate, self._request_reservation, self._request_decision = None, None, None
        self._approval_snapshots, self._snapshot_failed, self._approval_attempted = [], False, False
        self._approval_deadline_monitor_delivered = False
        self._approval_deadline_monitor_due = None
        self._approval_run_active = True
        self.approval_actor = WorkerApprovalActor(context=inflight["context"], scope=scope, revision=0,
            request_writer=request_writer, grant_reader=grant_reader, bounds=inflight["bounds"],
            admission=self._approval_admission, command=self._apply_approval, event=self._approval_event, clock=clock)
        self._inflight_ready = True
        self._publish_snapshot()

    def _approval_event(self, name, **fields):
        timestamp = fields.pop("time_s", self.s.time_s)
        self.events.append({"time_s": timestamp, "event": name, "detail": "", **fields})

    def reservation(self):
        if self.selected is None:
            return None
        # The reservation identifies the evidence frozen at selection. It is
        # not a fresh feasibility assertion, nor the mutable prediction table.
        # Re-hashing four coast traces on every physical tick defeats the fixed
        # wall budget. Current evidence is rechecked at selection and dispatch.
        revision = next((row for row in reversed(self.revisions)
            if row["revision"] == self.revision and row["candidate_id"] == self.selected), None)
        return {"candidate_id": self.selected,
                "candidate_sha256": revision["candidate_sha256"] if revision else None,
                "return_time_s": revision["candidate_snapshot"]["return_time_s"] if revision
                    else self.times.get(self.selected),
                "plan_revision": self.revision}

    def approval_snapshot(self):
        observed = self.observation()
        reservation = self.reservation()
        return {"run_active": self._approval_run_active and not self._snapshot_failed and self.dispatch is None,
            "simulation_time_s": self.s.time_s, "revision": self.scope_revision,
            "scope": deepcopy(self.active_scope), "reservation_sha256": digest(reservation),
            "reservation": reservation,
            "observation": {"state_sha256": digest(observed), "simulation_time_s": self.s.time_s,
                            "wall_time_s": self._approval_clock()}}

    def _publish_snapshot(self):
        state = self.approval_snapshot()
        self._approval_snapshots.append(deepcopy(state))
        try:
            self._snapshot_sink(deepcopy(state))
        except Exception:
            self._snapshot_failed = True
            self.event("m3_approval_telemetry_unavailable", authority_expansion_allowed=False)
        return state

    def step(self):
        super().step()
        if getattr(self, "_inflight_ready", False) and self.dispatch is None:
            state = self._publish_snapshot()
            self.approval_actor.tick(state)
            # A grant can have changed the scope/reservation during this tick.
            self._publish_snapshot()

    def _forecast_names(self, names):
        """A consumed candidate grant cannot authorize replacement predictions.

        Keep its original generation, origin and hash for the entire run. Fresh
        admission still compares current observations at selection and dispatch;
        retaining this evidence does not declare it physically valid.
        """
        names = list(names)
        if self._authorized_candidate is None or "next_orbit" not in names:
            return names
        self.event("m3_approved_prediction_preserved", candidate_id="next_orbit",
            candidate_sha256=self._authorized_candidate, scope_revision=self.scope_revision)
        return [name for name in names if name != "next_orbit"]

    def calculate(self, names, **kwargs):
        names = list(names)
        remaining = self._forecast_names(names)
        if not remaining:
            preserved = (bool(names) and set(names) == {"next_orbit"}
                and self._authorized_candidate is not None
                and digest(self.candidates.get("next_orbit")) == self._authorized_candidate)
            self.last_calculation_result = {"status": "reused" if preserved else "not_started",
                "reason": "candidate_bound_approval_preserved" if preserved else "no_forecast_names"}
            return preserved
        return super().calculate(remaining, **kwargs)

    def calculate_trials(self, names, **kwargs):
        # The optional comparison preparation can call this entry directly.
        names = list(names)
        remaining = self._forecast_names(names)
        if not remaining:
            preserved = (bool(names) and set(names) == {"next_orbit"}
                and self._authorized_candidate is not None
                and digest(self.candidates.get("next_orbit")) == self._authorized_candidate)
            self.last_calculation_result = {"status": "reused" if preserved else "not_started",
                "reason": "candidate_bound_approval_preserved" if preserved else "no_forecast_names"}
            return preserved
        return super().calculate_trials(remaining, **kwargs)

    def fixture_response(self, request):
        from src.intelligence.starship_replanning import fixture_decision

        return fixture_decision(request, self.envelope, inflight_contract=self.inflight_contract)

    def decision_context(self):
        snapshot = self._publish_snapshot()
        window = self.request_window(wall_time_s=snapshot["observation"]["wall_time_s"])
        return {"active_authority": {"schema": "missionos.inflight_active_authority.v2",
            "contract_sha256": digest(self.inflight_contract), "scope": deepcopy(self.active_scope),
            "scope_revision": self.scope_revision, "scope_sha256": digest(self.active_scope),
            "amendment_status": self.approval_actor.status,
            "technical_candidates_are_not_authorization": True,
            "select_next_orbit_requires_candidate_bound_operator_approval": self._authorized_candidate is None,
            "request_window": window}}

    def request_window(self, *, wall_time_s=None):
        return amendment_window(self.approval_actor.bounds, simulation_time_s=self.s.time_s,
            wall_time_s=self._approval_clock() if wall_time_s is None else wall_time_s,
            model_response_reserve_s=reserved_decision_s(self.envelope),
            status=self.approval_actor.status, scope_revision=self.scope_revision,
            attempted=self._approval_attempted, telemetry_available=not self._snapshot_failed)

    def constrain_decision_request(self, request):
        # Physical candidate eligibility is retained as evidence. An unavailable
        # authority request is not an actionable alternative, even if it would
        # be physically possible. No alternative is selected here.
        authority = request["active_authority"]
        window = authority["request_window"]
        if not self._scope_check("next_orbit")[0] and not window["request_available"]:
            request["allowed_actions"] = [a for a in request["allowed_actions"] if a != "select_next_orbit"]
        if not request["allowed_actions"]:
            request["allowed_actions"] = ["keep_plan" if self.selected else "wait_for_update"]
        warning_at = window["latest_decision_start_s"]-APPROVAL_COAST_GUARD_S
        scheduled_warning = self._approval_deadline_monitor_due
        if self.s.time_s >= warning_at or (scheduled_warning is not None and self.s.time_s >= scheduled_warning-1e-7):
            self._approval_deadline_monitor_delivered = True
        return request

    def next_monitor_time(self, due):
        window = self.request_window()
        if self.scope_revision or self._approval_attempted or self.approval_actor.status != "idle":
            return due
        if self._approval_deadline_monitor_delivered or not window["request_available"]:
            return due
        # Wake once before the last viable decision start. Extra coast guard
        # absorbs a physical integration step; this does not extend authority.
        lead = (window["model_response_reserve_s"] + window["operator_response_reserve_s"]
                + 2*window["coast_guard_s"])
        wake = min(window["simulation_deadline_s"],
                   self.s.time_s+window["remaining_wall_s"])-lead
        if wake <= self.s.time_s:
            self._approval_deadline_monitor_delivered = True
            return min(due, self.s.time_s+min(self.p["integration"]["coast_dt_s"], 2.001))
        if wake < due:
            self._approval_deadline_monitor_due = wake
        return min(due, wake)

    def _scope_check(self, name, *, dispatch=False):
        action = "select_"+name
        if action not in self.active_scope["commands"]:
            return False, "outside_active_return_scope"
        if name == "next_orbit":
            if self.selected != name:
                return False, "candidate_approval_is_single_use"
            if self._authorized_candidate != digest(self.candidates.get(name)):
                return False, "candidate_not_bound_to_consumed_approval"
            if self.approval_actor.status not in ({"reservation_confirmed"} if dispatch else
                    {"reservation_changed_waiting_observation", "reservation_confirmed"}):
                return False, "reservation_not_observed_after_approval"
        return True, None

    def dispatch_check(self, name, notice, observation):
        physical = super().dispatch_check(name, notice, observation)
        accepted, reason = self._scope_check(name, dispatch=True)
        candidate = self.candidates.get(name)
        candidate_sha256 = digest(candidate)
        reservation = self.reservation()
        if (reservation is None or reservation["candidate_sha256"] != candidate_sha256
                or candidate is None or reservation["return_time_s"] != candidate["return_time_s"]):
            accepted, reason = False, "dispatch_candidate_not_bound_to_reservation"
        coast_dt = min(self.p["integration"]["coast_dt_s"], 2.001)
        if (self.selected != name or candidate is None
                or self.times.get(name) != candidate["return_time_s"]
                or not -1e-7 <= self.s.time_s-candidate["return_time_s"] <= coast_dt+1e-7):
            accepted, reason = False, "dispatch_reservation_or_burn_time_changed"
        authority = {"accepted": accepted, "reason": reason,
            "scope_sha256": digest(self.active_scope), "scope_revision": self.scope_revision,
            "candidate_sha256": candidate_sha256,
            "consumption_sha256": self.approval_actor.applied["consumption_sha256"]
                if name == "next_orbit" and self.approval_actor.applied else None}
        self.event("m3_return_dispatch_authority_checked", candidate_id=name, check=authority)
        # Successful physical receipt remains byte-compatible with its scalar
        # verifier; authority has a separate mandatory gate and audit event.
        if accepted:
            return physical
        return {**physical, "accepted": False, "reasons": list(physical.get("reasons", []))+[reason]}

    def _approval_admission(self, action, state):
        if self._snapshot_failed:
            return {"accepted": False, "reason": "host_telemetry_unavailable"}
        name = "next_orbit" if action == "select_next_orbit" else self.selected
        if name not in self.candidates:
            return {"accepted": False, "reason": "candidate_or_booking_unavailable"}
        check = self.admit_candidate(self.candidates[name], notices(self.case, self.s.time_s), self.observation())
        if action == "select_next_orbit" and state["reservation_sha256"] != self._request_reservation:
            check = {**check, "accepted": False, "reason": "reservation_changed_while_approval_pending"}
        candidate_sha256 = digest(self.candidates[name])
        if action == "keep_reserved_return":
            reservation = self.reservation()
            if (reservation is None or reservation["candidate_sha256"] != candidate_sha256
                    or reservation["return_time_s"] != self.candidates[name]["return_time_s"]
                    or self.times.get(name) != reservation["return_time_s"]):
                check = {**check, "accepted": False, "reason": "fallback_reservation_evidence_changed"}
            elif not self._scope_check(name)[0]:
                check = {**check, "accepted": False, "reason": "fallback_outside_active_scope"}
        return {**check, "candidate_sha256": candidate_sha256}

    def _apply_approval(self, action, state, consumption):
        _require(action == "select_next_orbit" and state["revision"] == self.scope_revision
            and consumption["candidate_sha256"] == digest(self.candidates["next_orbit"])
            and consumption["revision"] == self.scope_revision+1 and self._authorized_candidate is None,
            "approved_candidate_or_revision_changed")
        check = self._approval_admission(action, self.approval_snapshot())
        _require(check["accepted"], "approved_candidate_no_longer_admitted")
        # Base select performs a second, current physical check and changes the
        # actual executor reservation. No prediction state is installed here.
        _require(super().select("next_orbit", self._request_basis), "executor_selection_rejected")
        self.active_scope, self.scope_revision = deepcopy(consumption["scope"]), consumption["revision"]
        self._authorized_candidate = consumption["candidate_sha256"]
        self.revisions[-1].update(approval_request_id=self.approval_actor.frame["request_id"],
            consumption_sha256=consumption["sha256"], scope_revision=self.scope_revision,
            scope_sha256=digest(self.active_scope), decision_request_id=self._request_decision,
            authority_basis="consumed_candidate_bound_operator_approval")
        return {"applied": True, "revision": self.scope_revision,
                "reservation_sha256": digest(self.reservation())}

    def select(self, name, basis):
        if self._scope_check(name)[0]:
            return super().select(name, basis)
        self.event("m3_return_selection_withheld", candidate_id=name, reason="outside_active_or_candidate_scope")
        actor = self.approval_actor
        if name != "next_orbit" or basis not in {"ai", "fixture"} or self._authorized_candidate is not None:
            return False
        if actor.status == "idle":
            if self._approval_attempted or name not in self.candidates:
                return False
            check = self.admit_candidate(self.candidates.get(name), notices(self.case, self.s.time_s), self.observation())
            if not check["accepted"] or self._snapshot_failed:
                return False
            self._request_basis = basis
            self._request_reservation = digest(self.reservation())
            self._request_decision = self.decisions[-1]["request"]["request_id"]
            state = self._publish_snapshot()
            if self._snapshot_failed:
                return False
            self._approval_attempted = True
            try:
                actor.request_change(request_id=f"{self.run_id}-inflight-1", candidate_sha256=digest(self.candidates[name]), state=state)
            except (InflightApprovalError, OSError):
                self.event("m3_scope_request_not_sent", reason="approval_original_bounds_or_channel")
                return False
        if actor.status == "awaiting_operator":
            bounds = actor.bounds
            limit = min(bounds["wall_deadline_s"]-self._approval_clock(),
                        bounds["simulation_deadline_s"]-self.s.time_s)
            if limit > 0:
                self.wait("inflight_operator_approval", lambda: actor.tick(self._publish_snapshot()), limit,
                          cutoff_time_s=bounds["simulation_deadline_s"])
            actor.tick(self._publish_snapshot())
            if (actor.status == "awaiting_operator"
                    and self.s.time_s+self.p["integration"]["coast_dt_s"] > bounds["simulation_deadline_s"]
                    and self.s.time_s+self.p["integration"]["coast_dt_s"] <= bounds["reservation_cutoff_s"]):
                self.step()
        return self.selected == name and self._scope_check(name)[0]

    def finish(self):
        self._approval_run_active = False
        self._publish_snapshot()
        result = super().finish()
        result["inflight_approval"] = {"contract": deepcopy(self.inflight_contract),
            "contract_sha256": digest(self.inflight_contract), "worker": self.approval_actor.record(),
            "active_scope": deepcopy(self.active_scope), "scope_revision": self.scope_revision,
            "authorized_candidate_sha256": self._authorized_candidate,
            "snapshots": deepcopy(self._approval_snapshots),
            "human_response_observed_by_worker": False, "worker_host_signature_verified": False,
            "operator_identity_evidence": "host_ledger_required"}
        return result


def _verify_request_evidence(record, contract, host, ledger):
    """Bind all dispositions to host-authenticated rows; do not verify HMAC here."""
    worker, frame = record["worker"], record["worker"]["request_frame"]
    context, bounds, initial = contract["context"], contract["bounds"], contract["initial_scope"]
    _require(host is not None and ledger is not None, "host_ledger_evidence_required")
    response, request, receipt, consumed = host["result"], ledger["request"], ledger["receipt"], ledger["consumption"]
    _require(frame["schema"] == "missionos.inflight_scope_request.v1"
        and frame["context"] == host["context"] == context and host["bounds"] == bounds
        and host["executor_command_issued"] is False
        and frame["request_id"] == context["run_id"]+"-inflight-1"
        and frame["action"] == "select_next_orbit" and frame["fallback"] == "keep_reserved_return"
        and frame["revision"] == 0 and frame["scope_sha256"] == digest(initial)
        and frame["simulation_deadline_s"] == bounds["simulation_deadline_s"]
        and frame["wall_deadline_s"] == bounds["wall_deadline_s"], "host_evidence_outside_original_contract")
    _require(response["schema"] == "missionos.inflight_host_result.v1"
        and response["context"] == context and response["request_id"] == frame["request_id"]
        and response["request_frame_sha256"] == digest(frame) and host["request_frame"] == frame
        and host["request"] == response["request"] == request
        and response["consumption"] == consumed
        and (worker["host_result"] is None or worker["host_result"] == response),
        "host_consumption_evidence_binding")
    for row, schema in ((request, "request"), (receipt, "receipt"), (consumed, "consumption")):
        _require(row["schema"] == "missionos.starship_inflight_approval_"+schema+".v1"
            and _hash(row["signature"]) and _hash(row["candidate_sha256"])
            and row["sha256"] == digest({k: v for k, v in row.items() if k not in {"sha256", "signature"}})
            and row["request_id"] == frame["request_id"] and row["mission_id"] == context["mission_id"]
            and row["vehicle_id"] == context["vehicle_id"]
            and row["candidate_sha256"] == frame["candidate_sha256"], "host_signed_record_binding")
    _require(request["current_revision"] == 0 and request["current_scope"] == initial
        and request["current_scope_sha256"] == digest(initial)
        and request["requested_scope_delta"] == {"add_commands": ["select_next_orbit"], "resource_limits": {}}
        and request["initial_observation"] == frame["observation"]
        and request["observed_state_sha256"] == frame["observation"]["state_sha256"]
        and request["maximum_observation_age_s"] == bounds["maximum_observation_age_s"]
        and request["simulation_deadline_s"] == frame["simulation_deadline_s"]
        and request["wall_deadline_s"] == frame["wall_deadline_s"]
        and request["fallback"] == frame["fallback"] and request["simulation_only"] is True
        and request["physical_execution_authorized"] is False
        and consumed["request_sha256"] == receipt["request_sha256"] == request["sha256"]
        and consumed["receipt_sha256"] == receipt["sha256"], "host_evidence_outside_original_contract")
    for row, simulation, wall in ((request, "issued_simulation_time_s", "issued_wall_time_s"),
                                   (receipt, "simulation_time_s", "wall_time_s"),
                                   (consumed, "simulation_time_s", "wall_time_s")):
        _require(_number(row[simulation]) and _number(row[wall]), "invalid_host_evidence_clock")
    _require(_number(frame["simulation_time_s"])
        and frame["simulation_time_s"] <= request["issued_simulation_time_s"] < frame["simulation_deadline_s"]
        and request["issued_wall_time_s"] < frame["wall_deadline_s"]
        and request["issued_simulation_time_s"] <= receipt["simulation_time_s"] <= consumed["simulation_time_s"]
        and request["issued_wall_time_s"] <= receipt["wall_time_s"] <= consumed["wall_time_s"],
        "host_evidence_clock_binding")
    receipt_expired = (receipt["simulation_time_s"] >= frame["simulation_deadline_s"]
                       or receipt["wall_time_s"] >= frame["wall_deadline_s"])
    expired = (consumed["simulation_time_s"] >= frame["simulation_deadline_s"]
               or consumed["wall_time_s"] >= frame["wall_deadline_s"])
    choice = receipt["choice"]
    _require(choice in {"approve", "reject", "expire"} and (choice == "expire") == receipt_expired
        and receipt["executor_command_issued"] is False and receipt["sso_identity_verified"] is False
        and receipt["local_operator_authenticated"] is (choice != "expire")
        and (receipt["operator_id"] is None if choice == "expire" else isinstance(receipt["operator_id"], str)
             and bool(receipt["operator_id"])), "host_receipt_disposition_binding")
    approved = choice == "approve" and not expired
    scope = {"commands": INITIAL_COMMANDS+["select_next_orbit"], "resource_limits": initial["resource_limits"]} if approved else initial
    _require(consumed["approved"] is approved and consumed["previous_revision"] == 0
        and consumed["revision"] == int(approved) and consumed["scope"] == scope
        and consumed["scope_sha256"] == digest(scope) and consumed["scope_applied"] is False
        and consumed["revision_semantics"] == "authorized_revision_not_applied_revision"
        and consumed["approval_disposition"] == ("approved_pending_executor" if approved else "not_approved")
        and consumed["reason"] == response["reason"] == ("approved" if approved else "expired" if expired else "rejected")
        and consumed["fallback"] == (None if approved else frame["fallback"])
        and consumed["requires_executor_check"] is True and consumed["simulation_only"] is True
        and consumed["executor_command_issued"] is False and consumed["physical_execution_authorized"] is False,
        "host_consumption_disposition_binding")
    for observation, simulation, wall in ((request["initial_observation"], request["issued_simulation_time_s"], request["issued_wall_time_s"]),
                                          (consumed["latest_observation"], consumed["simulation_time_s"], consumed["wall_time_s"])):
        _require(_hash(observation["state_sha256"]) and _number(observation["simulation_time_s"])
            and _number(observation["wall_time_s"])
            and 0 <= simulation-observation["simulation_time_s"] <= bounds["maximum_observation_age_s"]
            and 0 <= wall-observation["wall_time_s"] <= bounds["maximum_observation_age_s"],
            "host_observation_clock_binding")
    requested = [e for e in worker["events"] if e["event"] == "m3_scope_change_requested"]
    _require(len(requested) == 1 and requested[0]["request_id"] == frame["request_id"]
        and requested[0]["candidate_sha256"] == frame["candidate_sha256"]
        and requested[0]["request_frame_sha256"] == digest(frame)
        and requested[0]["time_s"] == frame["simulation_time_s"]
        and any(s["observation"] == frame["observation"] and s["simulation_time_s"] == frame["simulation_time_s"]
                and s["revision"] == 0 and s["scope"] == initial for s in record["snapshots"]),
        "request_observation_not_in_executor_trace")
    return consumed


def _verify_unapplied_request(record, contract, consumed):
    worker, initial = record["worker"], contract["initial_scope"]
    frame = worker["request_frame"]
    _require(worker["applied_reservation"] is None
        and worker["status"] in {"fallback_preserved", "fallback_not_admitted"}
        and not any(e["event"] in {"m3_return_reservation_changed", "m3_return_reservation_observed"}
                    for e in worker["events"])
        and all(s["scope"] == initial and s["revision"] == 0 for s in record["snapshots"]),
        "unapproved_scope_or_reservation")
    fallback = [e for e in worker["events"] if e["event"] == "m3_scope_update_not_applied"]
    _require(len(fallback) == 1 and fallback[0]["request_id"] == frame["request_id"]
        and fallback[0]["reservation_changed"] is False and fallback[0]["burn_dispatch_issued"] is False
        and type(fallback[0]["fallback_check"]["accepted"]) is bool
        and (worker["status"] == "fallback_preserved") == fallback[0]["fallback_check"]["accepted"],
        "negative_disposition_fallback_missing")
    event = fallback[0]
    snapshots = [s for s in record["snapshots"] if s["simulation_time_s"] == event["time_s"]
        and s["reservation_sha256"] == digest(s["reservation"]) == event["current_reservation_sha256"]]
    _require(bool(snapshots) and event["time_s"] >= frame["simulation_time_s"], "negative_disposition_trace_missing")
    if worker["host_result"] is None:
        # The worker checks its deadline before reading the host pipe. Expiry
        # may therefore be authenticated by the host without a worker receipt.
        _require(consumed["approved"] is False and consumed["reason"] == "expired"
            and event["reason"] == "original_deadline_expired", "negative_host_result_not_observed")
    if event["reason"] == "original_deadline_expired":
        _require(any(s["simulation_time_s"] >= frame["simulation_deadline_s"]
                     or s["observation"]["wall_time_s"] >= frame["wall_deadline_s"] for s in snapshots),
                 "negative_expiry_before_original_deadline")
    elif consumed["approved"] is False:
        _require(event["reason"] == "host_declined_or_invalidated", "negative_disposition_reason_mismatch")
    if worker["status"] == "fallback_preserved":
        booked = [s for s in record["snapshots"] if s["observation"] == frame["observation"]]
        _require(all(s["reservation_sha256"] == event["current_reservation_sha256"] for s in booked),
                 "negative_fallback_changed_reservation")


def verify_inflight_return(study, *, expected_contract, host_record=None, signed_ledger_record=None):
    """Scalar authority audit, composed with the ordinary return verifier.

    ``signed_ledger_record`` must come from host-owned ledger.status(), which
    authenticates signed rows. This routine does not hold the host HMAC key.
    A receipt alone, without applied reservation and later observation, fails.
    """
    result = {"schema": "missionos.inflight_executor_verification.v1", "passed": False,
        "issues": [], "scope_amendment_applied": False, "reservation_observed": False,
        "request_binding_verified": False, "negative_disposition_verified": False,
        "burn_dispatch_observed": study.get("dispatch") is not None,
        "physical_return_verified": False, "host_signature_verified_by_worker": False}
    try:
        validate_inflight_executor_contract(expected_contract, study["envelope"], run_id=study["run_id"],
                                            nominal_burn_s=study["opportunities"]["nominal"])
        if "fixture_policy" in expected_contract:
            for decision in study["decisions"]:
                response = decision.get("response")
                if response is not None:
                    _require(response.get("fixture_policy_sha256") == digest(expected_contract["fixture_policy"])
                        and response.get("model_inference_invoked") is False, "fixture_policy_response_binding")
        record = study["inflight_approval"]
        _require(record["contract"] == expected_contract and record["contract_sha256"] == digest(expected_contract),
                 "inflight_contract_binding")
        worker, initial = record["worker"], expected_contract["initial_scope"]
        _require(worker["context"] == expected_contract["context"] and worker["bounds"] == expected_contract["bounds"]
            and worker["initial_scope"] == initial and worker["initial_revision"] == 0,
            "worker_initial_authority_binding")
        for decision in study["decisions"]:
            request = decision["request"]
            authority = request.get("active_authority", {})
            if authority.get("schema") != "missionos.inflight_active_authority.v2":
                continue  # Frozen v1 records predate exposed request timing.
            window = authority["request_window"]
            _require(authority["contract_sha256"] == digest(expected_contract)
                and type(window["request_attempted"]) is bool
                and type(window["trusted_telemetry_available"]) is bool
                and _number(window["remaining_wall_s"])
                and window["remaining_wall_s"] <= expected_contract["bounds"]["wall_deadline_s"],
                "request_window_contract_binding")
            observed_wall = expected_contract["bounds"]["wall_deadline_s"]-window["remaining_wall_s"]
            expected = amendment_window(expected_contract["bounds"], simulation_time_s=request["time_s"],
                wall_time_s=observed_wall,
                model_response_reserve_s=reserved_decision_s(study["envelope"]),
                status=authority["amendment_status"], scope_revision=authority["scope_revision"],
                attempted=window["request_attempted"], telemetry_available=window["trusted_telemetry_available"])
            _require(digest(window) == digest(expected), "request_window_time_binding")
            # decision_context publishes this exact trusted executor snapshot.
            # A broad start/end bracket could hide an invented remaining budget.
            # The zero clamp is checked against the snapshot's actual clock too.
            _require(any(s["simulation_time_s"] == request["time_s"]
                and s["revision"] == authority["scope_revision"]
                and digest(s["scope"]) == authority["scope_sha256"]
                and max(0.0, expected_contract["bounds"]["wall_deadline_s"]
                        -s["observation"]["wall_time_s"]) == window["remaining_wall_s"]
                and (not window["request_available"] or s["run_active"])
                for s in record["snapshots"]), "request_window_observation_binding")
            if authority["scope_revision"] == 0 and "select_next_orbit" in request["allowed_actions"]:
                _require(window["request_available"]
                    and request["deadline_s"]+window["operator_response_reserve_s"]+window["coast_guard_s"]
                        <= window["simulation_deadline_s"]
                    and request["deadline_s"]-request["time_s"]+window["operator_response_reserve_s"]
                        +window["coast_guard_s"] <= window["remaining_wall_s"], "expired_scope_request_advertised")
        grant, candidate_hash = None, record["authorized_candidate_sha256"]
        frame = worker["request_frame"]
        if frame is not None:
            consumed = _verify_request_evidence(record, expected_contract, host_record, signed_ledger_record)
            result["request_binding_verified"] = True
            if candidate_hash is None:
                _verify_unapplied_request(record, expected_contract, consumed)
                result["negative_disposition_verified"] = True
        else:
            _require(worker["host_result"] is None and worker["applied_reservation"] is None
                and not any(e["event"] == "m3_scope_change_requested" for e in worker["events"])
                and signed_ledger_record is None
                and (host_record is None or host_record["request_frame"] is None and host_record["result"] is None),
                "amendment_request_evidence_missing")
        _require(worker["host_signature_verified_by_worker"] is False
            and worker["burn_dispatch_issued"] is False and worker["physical_return_verified"] is False,
            "worker_authority_claim_invalid")
        if candidate_hash is not None:
            _require(host_record is not None and signed_ledger_record is not None, "host_ledger_evidence_required")
            response, frame = worker["host_result"], worker["request_frame"]
            grant = response["consumption"]
            context, bounds = expected_contract["context"], expected_contract["bounds"]
            request, receipt = response["request"], signed_ledger_record["receipt"]
            _require(frame["context"] == response["context"] == host_record["context"] == context
                and frame["request_id"] == response["request_id"] == request["request_id"] == grant["request_id"]
                == receipt["request_id"] == context["run_id"]+"-inflight-1"
                and request["mission_id"] == grant["mission_id"] == receipt["mission_id"] == context["mission_id"]
                and request["vehicle_id"] == grant["vehicle_id"] == receipt["vehicle_id"] == context["vehicle_id"]
                and frame["simulation_deadline_s"] == request["simulation_deadline_s"] == bounds["simulation_deadline_s"]
                and frame["wall_deadline_s"] == request["wall_deadline_s"] == bounds["wall_deadline_s"]
                and frame["revision"] == request["current_revision"] == 0
                and frame["scope_sha256"] == request["current_scope_sha256"] == digest(initial)
                and request["current_scope"] == initial
                and request["fallback"] == frame["fallback"] == "keep_reserved_return"
                and request["initial_observation"] == frame["observation"]
                and request["observed_state_sha256"] == frame["observation"]["state_sha256"]
                and grant["request_sha256"] == receipt["request_sha256"] == request["sha256"]
                and grant["receipt_sha256"] == receipt["sha256"]
                and request["sha256"] == digest({k: v for k, v in request.items() if k not in {"sha256", "signature"}})
                and receipt["sha256"] == digest({k: v for k, v in receipt.items() if k not in {"sha256", "signature"}}),
                "host_evidence_outside_original_contract")

            _require(response == host_record["result"] and frame == host_record["request_frame"]
                and response["request"] == signed_ledger_record["request"]
                and grant == signed_ledger_record["consumption"]
                and signed_ledger_record["receipt"]["choice"] == "approve", "host_consumption_evidence_binding")
            _require(grant["approved"] is True and grant["candidate_sha256"] == candidate_hash
                and frame["candidate_sha256"] == candidate_hash and response["request_frame_sha256"] == digest(frame)
                and grant["previous_revision"] == 0 and grant["revision"] == 1
                and grant["scope"] == {"commands": INITIAL_COMMANDS+["select_next_orbit"],
                                      "resource_limits": initial["resource_limits"]}
                and grant["sha256"] == digest({k: v for k, v in grant.items() if k not in {"sha256", "signature"}}),
                "candidate_scope_consumption_binding")
            applied = worker["applied_reservation"]
            _require(applied["consumption_sha256"] == grant["sha256"]
                and applied["simulation_time_s"] < frame["simulation_deadline_s"]
                and grant["wall_time_s"] < frame["wall_deadline_s"], "applied_after_original_deadline")
            observations = [event for event in worker["events"] if event["event"] == "m3_return_reservation_observed"
                            and event.get("matched") is True]
            _require(len(observations) == 1 and observations[0]["time_s"] > applied["simulation_time_s"]
                and observations[0]["reservation_sha256"] == applied["reservation_sha256"]
                and worker["status"] == "reservation_confirmed", "later_reservation_not_confirmed")
            snapshots = record["snapshots"]
            _require(any(snapshot["observation"] == frame["observation"] and snapshot["revision"] == 0
                         and snapshot["scope"] == initial for snapshot in snapshots), "request_observation_not_in_executor_trace")
            _require(any(snapshot["simulation_time_s"] == observations[0]["time_s"]
                         and snapshot["revision"] == grant["revision"] and snapshot["scope"] == grant["scope"]
                         and snapshot["reservation_sha256"] == digest(snapshot["reservation"]) == applied["reservation_sha256"]
                         and snapshot["observation"]["state_sha256"] == observations[0]["observed_state_sha256"]
                         for snapshot in snapshots), "later_reservation_trace_mismatch")
            rows = [row for row in study["plan_revisions"] if row["candidate_id"] == "next_orbit"]
            _require(len(rows) == 1 and rows[0]["candidate_sha256"] == candidate_hash
                and rows[0]["candidate_sha256"] == digest(rows[0]["candidate_snapshot"])
                and rows[0]["consumption_sha256"] == grant["sha256"]
                and rows[0]["approval_request_id"] == frame["request_id"]
                and rows[0]["time_s"] == applied["simulation_time_s"], "actual_reservation_not_bound_to_approval")
            _require(digest(study["candidates"].get("next_orbit")) == candidate_hash
                and study["opportunities"]["next_orbit"] == rows[0]["candidate_snapshot"]["return_time_s"],
                "approved_prediction_replaced_after_consumption")
            _require(applied["reservation_sha256"] == digest({"candidate_id": "next_orbit",
                "candidate_sha256": candidate_hash, "return_time_s": rows[0]["candidate_snapshot"]["return_time_s"],
                "plan_revision": rows[0]["revision"]}), "applied_reservation_hash_mismatch")
            proposal = next(r for r in study["decisions"] if r["request"]["request_id"] == rows[0]["decision_request_id"])
            _require(proposal["accepted_action"] == "select_next_orbit", "proposal_provenance_missing")
            result.update(scope_amendment_applied=True, reservation_observed=True)
        else:
            _require(record["active_scope"] == initial and record["scope_revision"] == 0
                and not any(row["candidate_id"] == "next_orbit" for row in study["plan_revisions"]),
                "unapproved_scope_or_reservation")
        expected_scope = initial if grant is None else grant["scope"]
        _require(record["active_scope"] == expected_scope and worker["scope"] == expected_scope
            and record["scope_revision"] == worker["revision"] == (0 if grant is None else 1), "active_scope_result_mismatch")
        dispatch = study.get("dispatch")
        if dispatch is not None:
            name = dispatch["candidate_id"]
            checks = [event for event in study["execution"]["events"]
                      if event["event"] == "m3_return_dispatch_authority_checked"
                      and event["time_s"] == dispatch["time_s"] and event["candidate_id"] == name]
            _require(len(checks) == 1, "dispatch_authority_check_missing")
            check = checks[0]["check"]
            _require(check["accepted"] is True and "select_"+name in expected_scope["commands"]
                and check["scope_sha256"] == digest(expected_scope)
                and check["scope_revision"] == record["scope_revision"]
                and check["candidate_sha256"] == digest(study["candidates"][name]), "dispatch_outside_active_scope")
            if name == "next_orbit":
                _require(grant is not None and check["consumption_sha256"] == grant["sha256"]
                    and check["candidate_sha256"] == candidate_hash
                    and dispatch["time_s"] >= observations[0]["time_s"], "dispatch_before_observed_approval_effect")
        result["passed"] = True
    except (InflightApprovalError, KeyError, TypeError, ValueError, StopIteration) as exc:
        result["issues"].append(str(exc) if isinstance(exc, InflightApprovalError) else "malformed_inflight_execution_evidence")
    return result

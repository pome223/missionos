"""Incremental, opt-in continuation of a sealed actual return handoff.

The caller supplies immediate, bounded future submitters for host decisions,
numerical forecasts and a separate checker. No provider belongs in this worker.
This actor reuses the production return controller, command allocation and
finite plant step; it does not implement another guidance phase machine.

The source-bound outer registration owns permission, subprocess termination and
host transcript authentication. Embedded callback receipts are not proof of
native inference or an independent process by themselves.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import os
import time

from . import starship_final_sensor_prediction as prediction
from . import starship_ship_capability_observation as obs
from . import starship_ship_sensor_observation as sensors
from . import starship_sixdof as dyn
from .starship_final_faults import FinalFaultHarness
from .starship_final_handoff import FinalActualReturnHandoff
from .starship_ship_return import advance_plant, control_interval, record_sample

SCHEMA = "missionos.starship_final_sensor_execution.v1"
SCHEMA_V2 = "missionos.starship_final_sensor_execution.v2"
PUBLIC_SCHEMA = "missionos.starship_final_sensor_observation.v1"
JUDGMENT_SCHEMA = "missionos.starship_final_sensor_judgment.v1"
GUARD_SCHEMA = "missionos.starship_final_sensor_guard_receipt.v1"
GUARD_SCHEMA_V2 = "missionos.starship_final_sensor_guard_receipt.v2"
CONTINUE, ADOPT = prediction.CONTINUE, prediction.ADOPT
TRACK, DELEGATE = "request_independent_tracking", "request_deep_reasoning"
EFFECTS = {CONTINUE: "Retain the nominal capability assumption. This is not proof that return succeeds.",
    TRACK: "Request independent historical burn tracking; data arrives after 30 simulated seconds. No model change.",
    ADOPT: "Propose one change to the numerical accelerometer point estimate at the registered application time, only after independent admission.",
    DELEGATE: "Explicitly delegate the same request to deeper host reasoning within its original remaining deadline. No authority or renewed deadline."}
BASE_FACTS = frozenset({"time_s", "altitude_m", "fuel_gauge_kg", "accel_factor", "accel_band_low",
    "accel_band_high", "accel_samples", "accel_anomaly", "tracking_requested", "tracking_delay_s",
    "application_time_s", "tracking_factor", "tracking_band_low", "tracking_band_high",
    "tracking_intervals", "guard_reconsideration", "prior_continue_rejected", "prior_adopt_rejected"})
PREVIEW_FACTS = frozenset(f"{action}.{label}.{key}" for action in prediction.ACTIONS
    for label in prediction.POINT_LABELS
    for key in ("sampled_factor", "speed_mps", "tilt_deg", "body_rate_rad_s", "reserve_kg", "limits_met"))
BOOLEAN_FACTS = frozenset({"accel_anomaly", "tracking_requested", "prior_continue_rejected", "prior_adopt_rejected"})
PUBLIC_KEYS = {"schema", "run_id", "request_id", "stage", "issued_at_s", "deadline_s",
    "scope_sha256", "facts", "allowed_actions", "effects"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def numerical_backend(full_backend):
    """Explicit projection: six-field final runtime identity to two numerics."""
    require(type(full_backend) is dict and set(full_backend) == {
        "python", "platform", "machine", "numpy", "scipy", "executable_sha256"}
        and all(type(value) is str and value for value in full_backend.values())
        and prediction.is_hash(full_backend["executable_sha256"]),
        "final_sensor_full_backend_six_fields_required")
    return {name: full_backend[name] for name in ("numpy", "scipy")}


@dataclass(frozen=True)
class FinalSensorExecutionPolicy:
    mode: str = "fixture"
    sensor_seed: int = 0
    initial_decision_s: float = 35.
    forecast_s: float = 300.
    final_decision_s: float = 150.
    guard_s: float = 45.
    application_delay_s: float = 600.
    application_reserve_s: float = 2.
    maximum_reconsiderations: int = 0
    maximum_duration_s: float = 3600.
    maximum_wall_s: float = 7200.
    fallback: str = "retain_nominal"

    def __post_init__(self):
        require(self.mode in ("fixture", "live") and type(self.sensor_seed) is int and self.sensor_seed >= 0
            and type(self.maximum_reconsiderations) is int and self.maximum_reconsiderations in (0, 1)
            and self.fallback == "retain_nominal" and self.application_delay_s == 600.
            and self.application_reserve_s == 2.
            and all(prediction.number(x) and x > 0 for x in (self.initial_decision_s, self.forecast_s,
                self.final_decision_s, self.guard_s, self.maximum_duration_s, self.maximum_wall_s))
            and self.initial_decision_s <= 35. and self.forecast_s <= 300.
            and self.final_decision_s <= 150. and self.guard_s <= 45.
            and self.maximum_duration_s <= 3600. and self.maximum_wall_s <= 7200.
            and self.initial_decision_s+sensors.TRACKING["request_latency_s"]+self.forecast_s
                +self.final_decision_s+self.guard_s+self.application_reserve_s <= self.application_delay_s,
            "final_sensor_policy_budget_or_scope")


def validate_public_request(value):
    value = prediction.json_copy(value)
    require(type(value) is dict and set(value) == PUBLIC_KEYS and value["schema"] == PUBLIC_SCHEMA
        and type(value["run_id"]) is str and prediction._RUN.fullmatch(value["run_id"])
        and type(value["request_id"]) is str and prediction._RUN.fullmatch(value["request_id"])
        and value["stage"] in ("initial", "final") and prediction.is_hash(value["scope_sha256"])
        and all(prediction.number(value[key]) for key in ("issued_at_s", "deadline_s"))
        and 0 <= value["issued_at_s"] < value["deadline_s"] <= value["issued_at_s"]+150.
        and type(value["facts"]) is dict and value["facts"].keys() <= BASE_FACTS | PREVIEW_FACTS
        and {"time_s", "accel_factor", "accel_band_low", "accel_band_high", "application_time_s"}
            <= value["facts"].keys()
        and value["facts"]["time_s"] == value["issued_at_s"]
        and all(type(x) is bool if key in BOOLEAN_FACTS or key.endswith(".limits_met") else prediction.number(x)
            for key, x in value["facts"].items()),
        "final_sensor_public_projection")
    actions = value["allowed_actions"]
    eligible = {CONTINUE, TRACK} if value["stage"] == "initial" else {CONTINUE, ADOPT, DELEGATE}
    require(type(actions) is list and actions and len(set(actions)) == len(actions)
        and all(type(action) is str and action in eligible for action in actions)
        and value["effects"] == {action: EFFECTS[action] for action in actions},
        "final_sensor_public_actions")
    return value


def validate_judgment(value, request, mode):
    require(type(value) is dict and set(value) == {"schema", "request_sha256", "action", "mode", "provenance", "decision_id"}
        and value["schema"] == JUDGMENT_SCHEMA and value["request_sha256"] == obs.digest(request)
        and value["action"] in request["allowed_actions"] and value["action"] != DELEGATE
        and value["mode"] == mode and type(value["provenance"]) is dict
        and value["provenance"].get("model_inference_invoked") is (mode == "live")
        and value["decision_id"] == obs.digest({k: v for k, v in value.items() if k != "decision_id"}),
        "final_sensor_judgment_binding")
    return prediction.json_copy(value)


def check_guard_bundle(bundle, authority):
    """Saved-only compatibility entry; production calls the separate checker."""
    from .starship_final_sensor_guard import check_bundle
    return check_bundle(bundle, authority)


class FinalSensorExecution:
    """Call step_once without sleeping; waiting never advances a plant.

    Submitters have signature ``callback(payload, *, deadline_monotonic)`` and
    must immediately return a Future with done/result/cancel. The host owns hard
    subprocess/provider timeouts; this actor additionally rejects late results.
    Normal continue-only calls are regression evidence, not fault diagnosis.
    """
    def __init__(self, handoff, fault_harness, profile, *, run_id, source_sha256, backend,
                 policy, decide, forecast, guard, clock=time.monotonic, integration_policy=None):
        require(type(handoff) is FinalActualReturnHandoff and type(fault_harness) is FinalFaultHarness
            and type(policy) is FinalSensorExecutionPolicy and callable(clock)
            and all(callable(x) for x in (decide, forecast, guard))
            and type(run_id) is str and prediction._RUN.fullmatch(run_id), "final_sensor_actual_owner_required")
        self.profile, self.source = prediction.json_copy(profile), prediction.json_copy(source_sha256)
        self.backend, self.policy, self.run_id = prediction.json_copy(backend), policy, run_id
        self.integration_fields = prediction.integration_fields(integration_policy)
        self.integration_policy = (prediction.RestoredIntegrationPolicy(2) if integration_policy is None
            else prediction.RestoredIntegrationPolicy.from_record(integration_policy.record()))
        self._check_source()
        self.clock, self.callbacks = clock, {"decision": decide, "forecast": forecast, "guard": guard}
        self.started_wall, self.handoff = clock(), handoff.record()
        require(prediction.number(self.started_wall), "final_sensor_wall_epoch")
        self.state, self.plant = fault_harness.consume_actual_handoff(handoff)
        self.harness = fault_harness
        self.initial_state, self.actual_vehicle = asdict(self.state), asdict(self.plant)
        self.inventory = prediction.json_copy(self.handoff["inventory"])
        self.nominal = prediction.nominal_from_actual(self.plant, self.profile, self.inventory["retained_count"])
        binding = self.handoff["snapshot"]["binding"]
        model = obs.model_vehicle(self.nominal, 1.)
        self.controller = obs.SwitchingController(self.nominal, model, self.profile,
            binding["return_time_s"], self.inventory["released_count"], obs.SwitchAudit(self.nominal, model, self.plant))
        self.initial_controller = self.controller.controller.to_dict()
        self.scope = obs.digest({"run_id": run_id, "source_sha256": self.source, "backend": self.backend,
            "policy": asdict(policy), **self.integration_fields,
            "handoff_sha256": obs.digest(self.handoff), "profile": self.profile})
        self.samples, self.events, self.truth_rows, self.tracking_rows = [], [], [], []
        self.sensor_source_rows, self.detection = [], None
        self.telemetry, self.delivery, self.complete_estimate, self.tracking_estimate = [], None, None, None
        self.complete_time, self.application_time, self.epoch, self.complete_snapshot = None, None, None, None
        self.cutoff, self.next_sample = None, self.state.time_s
        self.stage, self.pending, self.approved, self.application = "burn", None, None, None
        self.application_witness = None
        self.decisions, self.batches, self.guards, self.fallbacks = [], [], [], []
        self.steps, self.contact, self.error, self.termination = 0, None, None, None
        self.actual_execution_trace = []
        self._trace_actual("accepted_handoff")
        self.reconsiderations, self.final_decision_deadline = 0, None
        self.declined_work, self.nonintervention_regression = False, False

    def _trace_actual(self, boundary, *, force=False):
        """Separate actual-owner witnesses, never reconstructed from sensor rows."""
        phase = self.controller.phase
        if not force and getattr(self, "_last_trace_state", None) is self.state \
                and getattr(self, "_last_trace_vehicle", None) is self.plant \
                and getattr(self, "_last_trace_phase", None) == phase:
            return  # Both immutable objects are unchanged while a future waits.
        state = prediction.json_copy(asdict(self.state))
        vehicle_sha = (self._last_trace_vehicle_sha if getattr(self, "_last_trace_vehicle", None) is self.plant
            else obs.digest(asdict(self.plant)))
        self._last_trace_state, self._last_trace_vehicle, self._last_trace_vehicle_sha = self.state, self.plant, vehicle_sha
        self._last_trace_phase = phase
        if not force and self.actual_execution_trace and self.actual_execution_trace[-1]["state"] == state \
                and self.actual_execution_trace[-1]["vehicle_sha256"] == vehicle_sha \
                and self.actual_execution_trace[-1]["phase"] == phase:
            return
        self.actual_execution_trace.append({"sequence": len(self.actual_execution_trace)+1,
            "boundary": boundary, "state": state, "vehicle_sha256": vehicle_sha, "phase": phase})

    def _check_source(self):
        require("src/runtime/starship_final_sensor_execution.py" in self.source
            and all(prediction.current_source_checks(self.source, self.backend).values()),
            "final_sensor_execution_source_or_backend_changed")

    def _event(self, name, detail="", **extra):
        self.events.append({"time_s": self.state.time_s, "event": name, "detail": detail, **extra})
        if name == "deorbit_cutoff_command":
            require(self.cutoff is None, "final_sensor_multiple_deorbit_cutoffs")
            self.cutoff = self.state.time_s

    def _estimate(self):
        self.telemetry = sensors.measure_accelerometer(self.truth_rows, 0, self.policy.sensor_seed,
            gain=self.harness.accelerometer_gain(self.state.time_s))
        return obs.estimate(obs.prepare_telemetry(self.telemetry, self.nominal), self.state.time_s)

    def _capture(self):
        return prediction.make_snapshot(self.state, self.plant, self.controller, self.profile,
            run_id=self.run_id, handoff_sha256=obs.digest(self.handoff), inventory=self.inventory,
            cutoff_time_s=self.cutoff, complete_time_s=self.complete_time, application_time_s=self.application_time,
            integration_policy=self.integration_policy if self.integration_fields else None)

    def _public(self, stage, deadline_sim_s):
        current = self._estimate()
        facts = {"time_s": self.state.time_s, "altitude_m": dyn.observe(self.state, self.nominal)["altitude_m"],
            "fuel_gauge_kg": obs.observation(self.state, self.nominal, "complete_burn")["fuel_gauge_kg"],
            "accel_factor": current["factor_estimate"], "accel_band_low": current["band"][0],
            "accel_band_high": current["band"][1], "accel_samples": current["accepted_samples"],
            "accel_anomaly": current["anomaly"], "tracking_requested": self.delivery is not None,
            "tracking_delay_s": sensors.TRACKING["request_latency_s"], "application_time_s": self.application_time,
            "guard_reconsideration": self.reconsiderations,
            "prior_continue_rejected": bool(self.guards and self.guards[-1]["bundle"]["selected_action"] == CONTINUE),
            "prior_adopt_rejected": bool(self.guards and self.guards[-1]["bundle"]["selected_action"] == ADOPT)}
        if stage == "initial":
            actions = [CONTINUE, TRACK] if current["anomaly"] else [CONTINUE]
        else:
            self.tracking_estimate = sensors.estimate_tracking(self.delivery, self.nominal, self.state.time_s)
            tracking = self.tracking_estimate
            facts.update(tracking_factor=tracking["factor_estimate"], tracking_band_low=tracking["band"][0],
                tracking_band_high=tracking["band"][1], tracking_intervals=tracking["accepted_samples"])
            facts.update(prediction.forecast_evidence(self.batches[-1]["forecasts"], self.batches[-1]["snapshot"]))
            actions = [CONTINUE, ADOPT]
            if deadline_sim_s-self.state.time_s >= 150.-1e-8:
                actions.append(DELEGATE)
        value = {"schema": PUBLIC_SCHEMA, "run_id": self.run_id,
            "request_id": f"{self.run_id}-{stage}-{len(self.decisions)+1}", "stage": stage,
            "issued_at_s": self.state.time_s, "deadline_s": deadline_sim_s,
            "scope_sha256": self.scope, "facts": facts, "allowed_actions": actions,
            "effects": {action: EFFECTS[action] for action in actions}}
        return validate_public_request(value)

    def _submit(self, kind, payload, seconds, session):
        now = self.clock()
        deadline = min(self.epoch+self.policy.application_delay_s-self.policy.application_reserve_s, now+seconds)
        if kind == "decision" and payload["stage"] == "final":
            if self.final_decision_deadline is None:
                self.final_decision_deadline = deadline
            deadline = min(deadline, self.final_decision_deadline)
        session.update(started_wall_s=now, wall_deadline_s=deadline, status="pending", response=None)
        try:
            require(now < deadline, "final_sensor_task_deadline")
            future = self.callbacks[kind](prediction.json_copy(payload), deadline_monotonic=deadline)
            require(all(callable(getattr(future, name, None)) for name in ("done", "result", "cancel")),
                "final_sensor_bounded_future_required")
            self.pending = {"kind": kind, "future": future, "session": session}
        except Exception as exc:
            session.update(status="submit_error", error_type=type(exc).__name__)
            self._fallback(kind+"_submit_error")

    def _decision(self, stage):
        seconds = self.policy.initial_decision_s if stage == "initial" else self.policy.final_decision_s
        if stage == "final" and self.final_decision_deadline is not None:
            seconds = min(seconds, self.final_decision_deadline-self.clock())
        seconds = min(seconds, self.application_time-self.policy.application_reserve_s-self.state.time_s)
        require(seconds > 0, "final_sensor_decision_window_spent")
        request = self._public(stage, self.state.time_s+seconds)
        session = {"request": request, "request_sha256": obs.digest(request),
            "request_snapshot": self._capture(), "accepted_action": None}
        self.decisions.append(session)
        self.stage = stage+"_decision"
        self._submit("decision", request, seconds, session)

    def _start_forecasts(self):
        snapshot = self._capture()
        accel = self._estimate()
        tracking = sensors.estimate_tracking(self.delivery, self.nominal, self.state.time_s)
        self.tracking_estimate = tracking
        points = prediction._estimate(tracking, self.state.time_s)
        jobs = [{"snapshot": snapshot, "action": action, "sample_label": label,
            "sampled_factor": points[label], "accelerometer_estimate": accel, "sampling_estimate": tracking,
            "source_sha256": self.source, "backend": self.backend, **self.integration_fields,
            "timeout_s": min(180., self.policy.forecast_s)}
            for action in prediction.ACTIONS for label in prediction.POINT_LABELS]
        batch = {"snapshot": snapshot, "accelerometer_estimate": accel, "sampling_estimate": tracking,
            "jobs": jobs, "forecasts": None}
        self.batches.append(batch)
        self.stage = "forecast"
        self._submit("forecast", {"jobs": jobs}, self.policy.forecast_s, batch)

    def _start_guard(self, decision):
        current = self._capture()
        request = self.decisions[-1]["request"]
        authority = {"schema": "missionos.starship_final_sensor_guard_authority.v2" if self.integration_fields
            else "missionos.starship_final_sensor_guard_authority.v1", **self.integration_fields, "run_id": self.run_id,
            "source_sha256": self.source, "backend": self.backend, "scope_sha256": self.scope, "mode": self.policy.mode,
            "handoff_sha256": obs.digest(self.handoff), "inventory_sha256": obs.digest(self.inventory),
            "snapshot_id": self.batches[-1]["snapshot"]["snapshot_id"],
            "application_time_s": self.application_time, "contact_limits": prediction.CONTACT_LIMITS}
        bundle = {"schema": "missionos.starship_final_sensor_guard_bundle.v2" if self.integration_fields
            else "missionos.starship_final_sensor_guard_bundle.v1", **self.integration_fields, "run_id": self.run_id,
            "authority_sha256": obs.digest(authority), "request": request, "decision": decision,
            "selected_action": decision["action"], "snapshot": self.batches[-1]["snapshot"],
            "request_snapshot": self.decisions[-1]["request_snapshot"], "current_snapshot": current,
            "accelerometer_telemetry": self.telemetry,
            "tracking_delivery": self.delivery, "forecasts": self.batches[-1]["forecasts"],
            "checked_at_s": self.state.time_s, "source_sha256": self.source, "backend": self.backend}
        session = {"bundle": prediction.json_copy(bundle), "authority": prediction.json_copy(authority)}
        self.guards.append(session)
        self.stage = "guard"
        self._submit("guard", {"bundle": bundle, "authority": authority}, self.policy.guard_s, session)

    def _guard_result(self, response, session):
        require(type(response) is dict and set(response) == {"receipt", "process_evidence"},
            "final_sensor_guard_process_response")
        receipt, process = response["receipt"], response["process_evidence"]
        bundle, authority = session["bundle"], session["authority"]
        require(type(receipt) is dict and set(receipt) == {"schema", "bundle_sha256", "authority_sha256",
            "snapshot_id", "selected_action", "application_time_s", "factor_estimate", "passed", "checks", "receipt_id"}
            and receipt["schema"] == (GUARD_SCHEMA_V2 if self.integration_fields else GUARD_SCHEMA)
            and receipt["bundle_sha256"] == obs.digest(bundle)
            and receipt["authority_sha256"] == obs.digest(authority)
            and receipt["snapshot_id"] == authority["snapshot_id"]
            and receipt["selected_action"] == bundle["selected_action"]
            and receipt["application_time_s"] == self.application_time
            and receipt["factor_estimate"] == (bundle["request"]["facts"]["accel_factor"]
                if bundle["selected_action"] == ADOPT else 1.)
            and type(receipt["checks"]) is dict and receipt["checks"]
            and all(type(x) is bool for x in receipt["checks"].values())
            and type(receipt["passed"]) is bool and receipt["passed"] == all(receipt["checks"].values())
            and receipt["receipt_id"] == obs.digest({k: v for k, v in receipt.items() if k != "receipt_id"}),
            "final_sensor_guard_receipt_binding")
        require(type(process) is dict and type(process.get("checker_pid")) is int
            and process["checker_pid"] > 0 and process["checker_pid"] != os.getpid()
            and type(process.get("exit_code")) is int and process["exit_code"] == 0
            and process.get("credential_free") is True
            and (self.policy.mode == "fixture" or process.get("fixture_only") is False),
            "final_sensor_independent_checker_required")
        if receipt["passed"]:
            self.approved = {"receipt": deepcopy(receipt), "decision_id": bundle["decision"]["decision_id"],
                "mode": bundle["decision"]["mode"], "consumed": False}
            self.stage = "resolved"
        else:
            self._event("final_sensor_proposal_rejected", selected_action=bundle["selected_action"],
                receipt_id=receipt["receipt_id"], judgment_replaced=False)
            remaining = (self.final_decision_deadline or 0.)-self.clock()
            if self.reconsiderations < self.policy.maximum_reconsiderations and remaining > self.policy.guard_s+2.:
                self.reconsiderations += 1
                self._decision("final")
            else:
                self.stage = "guard_rejected"

    def _service(self):
        if self.pending is None:
            return
        pending = self.pending
        future, session, kind = pending["future"], pending["session"], pending["kind"]
        try:
            if self.clock() >= session["wall_deadline_s"] or self.state.time_s >= self.application_time-self.policy.application_reserve_s:
                try:
                    future.cancel()
                except Exception as exc:
                    session["cancel_error_type"] = type(exc).__name__
                session["status"] = "expired"
                self.pending = None
                self._fallback(kind+"_timeout")
                return
            if not future.done():
                return
        except Exception as exc:
            session.update(status="infrastructure_error", error_type=type(exc).__name__)
            self.pending = None
            self._fallback(kind+"_infrastructure_error")
            return
        self.pending = None
        try:
            response = prediction.json_copy(future.result())
            session.update(response=response, status="received", received_wall_s=self.clock())
            if self.clock() >= session["wall_deadline_s"]:
                session.update(status="late_response", late_response_adopted=False)
                self._fallback(kind+"_late_response")
                return
            if kind == "decision":
                decision = validate_judgment(response, session["request"], self.policy.mode)
                session.update(status="accepted", accepted_action=decision["action"])
                if session["request"]["stage"] == "initial":
                    if decision["action"] == TRACK:
                        measured = sensors.measure_tracking(self.tracking_rows, self.policy.sensor_seed)
                        self.delivery = sensors.request_tracking(measured, self.state.time_s, obs.digest(self.tracking_rows))
                        self.stage = "tracking_wait"
                    else:
                        self.declined_work = bool(self.complete_estimate["anomaly"])
                        self.nonintervention_regression = not self.declined_work
                        self.stage = "resolved"
                else:
                    self._start_guard(decision)
            elif kind == "forecast":
                prediction.forecast_preview(response, session["snapshot"])
                session["forecasts"] = response
                self._decision("final")
            else:
                self._guard_result(response, session)
        except Exception as exc:
            session.update(status="invalid", error_type=type(exc).__name__)
            self._fallback(kind+"_invalid_or_unavailable")

    def _fallback(self, reason):
        self.fallbacks.append({"time_s": self.state.time_s, "wall_s": self.clock(), "reason": reason,
            "action": CONTINUE, "basis": "source_registered_retain_nominal", "model_judgment": False,
            "model_replacement_claimed": False, "control_model_changed": False})
        self.stage = "fallback"

    def _numeric_progress(self):
        if self.termination is not None:
            return
        if self.complete_time is None and self.cutoff is not None and self.state.time_s >= self.cutoff+2.-1e-8:
            require(math.isclose(self.state.time_s, self.cutoff+2., abs_tol=1e-8), "final_sensor_complete_boundary_missed")
            self.complete_time, self.epoch = self.state.time_s, self.clock()
            self.application_time = self.complete_time+self.policy.application_delay_s
            self.complete_estimate = self._estimate()
            self.complete_snapshot = self._capture()
            try:
                prediction._estimate(self.complete_estimate, self.state.time_s)
                self._decision("initial")
            except (ValueError, TypeError, KeyError):
                self._fallback("complete_estimate_unavailable")
        if self.stage == "tracking_wait" and self.state.time_s >= self.delivery["received_at_s"]-1e-8:
            self._start_forecasts()

    def _apply(self):
        if self.application_time is None or self.state.time_s < self.application_time-1e-9:
            return
        if self.pending is not None:
            self._service()
        if self.approved is None or self.approved["consumed"]:
            return
        require(math.isclose(self.state.time_s, self.application_time, rel_tol=0., abs_tol=1e-8),
            "final_sensor_application_boundary_missed")
        self._check_source()
        receipt = self.approved["receipt"]
        self.application_witness = prediction.json_copy({"state": asdict(self.state),
            "controller": self.controller.controller.to_dict(), "actual_vehicle": asdict(self.plant),
            "inventory_sha256": obs.digest(self.inventory)})
        before = {"state_sha256": obs.digest(asdict(self.state)),
            "controller_sha256": obs.digest(self.controller.controller.to_dict())}
        self.approved["consumed"] = True
        if receipt["selected_action"] == ADOPT:
            self.controller.apply_switch(self.state.time_s, receipt["factor_estimate"], {
                "kind": "final_sensor_guarded_numeric_estimate", "receipt_id": receipt["receipt_id"],
                "snapshot_id": receipt["snapshot_id"]})
        self.application = {"time_s": self.state.time_s, "factor": receipt["factor_estimate"],
            "action": receipt["selected_action"], "snapshot_id": receipt["snapshot_id"],
            "consumed_receipt_id": receipt["receipt_id"], "decision_id": self.approved["decision_id"],
            "mode": self.approved["mode"], **before, "control_model_changed": receipt["selected_action"] == ADOPT}
        self._event("final_sensor_once_only_application", **self.application)

    def step_once(self):
        """At most one finite actual plant step, or a nonblocking waiting result."""
        if self.termination is not None:
            return {"advanced": False, "waiting": False, "complete": True}
        try:
            require(self.clock() >= self.started_wall, "final_sensor_wall_clock_regressed")
            self._trace_actual("step_entry")
            if self.clock() >= self.started_wall+self.policy.maximum_wall_s:
                self.termination = "execution_wall_budget"
                return {"advanced": False, "waiting": False, "complete": True}
            self._service()
            self._numeric_progress()
            self._apply()
            if self.state.time_s >= self.initial_state["time_s"]+self.policy.maximum_duration_s:
                self.termination = "time_limit"
                return {"advanced": False, "waiting": False, "complete": True}
            observed = dyn.observe(self.state, self.plant)
            # While any decision work is possible, actual coast follows the one
            # original wall epoch. Do not manufacture coast by assigning time.
            paced = self.complete_time is not None and self.state.time_s < self.application_time-1e-9
            allowance = self.complete_time+max(0., self.clock()-self.epoch) if paced else math.inf
            base_dt = control_interval(self.profile, 0., observed["altitude_m"])
            boundary = self.application_time if self.application_time is not None else (
                self.cutoff+2. if self.cutoff is not None else math.inf)
            if self.stage == "tracking_wait":
                boundary = min(boundary, self.delivery["received_at_s"])
            due_dt = min(base_dt, boundary-self.state.time_s)
            if paced and self.state.time_s+max(0., due_dt) > allowance+1e-8:
                return {"advanced": False, "waiting": True, "complete": False}
            step = self.controller.update(self.state, self.samples, self._event)
            self.state = step.state
            self._trace_actual("after_shared_controller_update")
            if step.transition_only:
                return {"advanced": False, "waiting": False, "complete": False}
            command, diagnostics = self.controller.command(step, observed["altitude_m"])
            self.next_sample = record_sample(self.state, self.plant, self.controller.phase,
                command, diagnostics, self.samples, self.next_sample, self.profile)
            require(math.sqrt(sum(x*x for x in self.state.omega_body_rad_s)) <= 5., "final_sensor_angular_rate_envelope")
            dt = min(control_interval(self.profile, step.throttle, observed["altitude_m"]),
                self.initial_state["time_s"]+self.policy.maximum_duration_s-self.state.time_s)
            if self.cutoff is not None and self.complete_time is None:
                dt = min(dt, self.cutoff+2.-self.state.time_s)
            if self.application_time is not None and self.state.time_s < self.application_time-1e-9:
                dt = min(dt, self.application_time-self.state.time_s)
            if self.stage == "tracking_wait":
                dt = min(dt, self.delivery["received_at_s"]-self.state.time_s)
            require(dt > 0 and (not paced or self.state.time_s+dt <= allowance+1e-8),
                "final_sensor_step_or_pacer_boundary")
            self.controller.audit.inspect("plant", self.state, self.plant)
            self._trace_actual("before_finite_actual_plant_step", force=True)
            if self.controller.phase == "deorbit_burn":
                self.sensor_source_rows.append({"state": prediction.json_copy(asdict(self.state)),
                    "phase": self.controller.phase})
                self.truth_rows.append(obs.specific_force_row(self.state, self.plant, self.controller.phase))
                self.tracking_rows.append(sensors.tracking_source_row(self.state, self.controller.phase))
                current = self._estimate()
                if self.detection is None and current["anomaly"]:
                    self.detection = current
                    self._event("final_sensor_numeric_anomaly_detected", estimate_id=current["estimate_id"])
            self.state, contact = advance_plant(self.state, self.plant, command, dt,
                self.profile["geometry"]["ship_length_m"], self.profile["geometry"]["radius_m"],
                observed["altitude_m"], observed["air_speed_mps"])
            self.steps += 1
            self._trace_actual("after_finite_actual_plant_step")
            if contact is not None and contact["contact"]:
                self.contact = contact
                self.termination = "surface_impact" if contact["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
            return {"advanced": True, "waiting": False, "complete": self.termination is not None}
        except (ValueError, TypeError, KeyError, OverflowError, FloatingPointError, InterruptedError) as exc:
            self.termination, self.error = "numerical_or_control_failure", {"type": type(exc).__name__, "detail": str(exc)[:160]}
            return {"advanced": False, "waiting": False, "complete": True}

    def snapshot(self):
        return prediction.json_copy({"state": asdict(self.state), "vehicle": asdict(self.plant),
            "controller": self.controller.controller.to_dict(), "inventory": self.inventory,
            "control_stage": self.stage, "complete": self.termination is not None})

    def result(self):
        require(self.termination is not None, "final_sensor_execution_not_finished")
        # Actual capability is reconstructed ONLY for the private saved audit,
        # after execution. It never selects a model input, estimate or action.
        ratios = [self.plant.engines[i].max_thrust_n/self.nominal.engines[i].max_thrust_n for i in obs.MAIN]
        require(all(math.isclose(x, ratios[0], rel_tol=0., abs_tol=1e-12) for x in ratios),
            "final_sensor_saved_audit_nonuniform_actual_capability")
        from .starship_sixdof_mission import _sample
        samples = deepcopy(self.samples)
        samples.append(_sample(self.state, self.plant, self.controller.phase))
        run = {"state": self.state, "samples": samples, "events": self.events,
            "termination": self.termination, "contact": self.contact, "error": self.error, "steps": self.steps}
        switch = self.controller.switch
        rollout = obs.record("final_actual_sensor_return", self.initial_state, self.plant, self.nominal,
            self.controller, run, plant_factor=ratios[0], switch=None if switch is None else {
                "factor": switch["factor"], "time_s": switch["time_s"]}, wall_time_s=self.clock()-self.started_wall)
        # Raw six-point trajectories appear once in saved suffix evidence. The
        # exact callback and separate checker preimage remain reconstructable;
        # neither hash is a signature or a substitute for the owning task log.
        batches = []
        for batch in self.batches:
            saved = {key: value for key, value in batch.items() if key != "response"}
            saved["response_sha256"] = None if batch["response"] is None else obs.digest(batch["response"])
            batches.append(saved)
        guards = []
        for session in self.guards:
            bundle = session["bundle"]
            saved_bundle = {key: value for key, value in bundle.items() if key != "forecasts"}
            saved_bundle["forecasts_sha256"] = obs.digest(bundle["forecasts"])
            saved_bundle["forecast_batch_snapshot_id"] = bundle["snapshot"]["snapshot_id"]
            guards.append({**session, "bundle": saved_bundle})
        return prediction.json_copy({"schema": SCHEMA_V2 if self.integration_fields else SCHEMA,
            **self.integration_fields, "run_id": self.run_id, "profile": self.profile,
            "source_sha256": self.source, "backend": self.backend, "policy": asdict(self.policy),
            "scope_sha256": self.scope, "handoff_record": self.handoff, "handoff_sha256": obs.digest(self.handoff),
            "inventory": self.inventory, "inventory_receipt_chain_sha256": obs.digest(self.inventory["release_receipts"]),
            "fault_harness": self.harness.record(), "initial_state": self.initial_state,
            "actual_vehicle": self.actual_vehicle, "nominal_vehicle": asdict(self.nominal),
            "initial_controller": self.initial_controller, "final_controller": self.controller.controller.to_dict(),
            "accelerometer_telemetry": self.telemetry, "tracking_source_rows": self.tracking_rows,
            "sensor_source_rows": self.sensor_source_rows, "detection": self.detection,
            "actual_execution_trace": self.actual_execution_trace,
            "tracking_delivery": self.delivery, "complete_estimate": self.complete_estimate,
            "tracking_estimate": self.tracking_estimate, "complete_snapshot": self.complete_snapshot,
            "decision_sessions": self.decisions, "forecast_batches": batches, "guard_sessions": guards,
            "application": self.application, "fallback_ledger": self.fallbacks, "control_stage": self.stage,
            "application_witness": self.application_witness,
            "declined_work": self.declined_work, "nonintervention_regression": self.nonintervention_regression,
            "rollout": rollout, "claim_limits": {"model_calls_by_worker": 0,
                "native_judgments_require_external_host_transcript": True,
                "independent_checker_requires_external_process_transcript": True,
                "physical_execution": False, "qualification_refreshed": False,
                "new_cohort_application_boundary_and_wall_pacing": True}})

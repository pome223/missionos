"""Opt-in symptom decisions in the same M3 return, without replacing its state.

The private test harness alters the plant once, at the dispatched return. All
controller inputs still use a distinct nominal model. Observation acquisition,
provider decisions, numerical previews, Rules receipts and actuation are logged
as separate events. Transport waits advance the existing finite plant.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import math
from threading import Event
import time

from . import starship_ship_capability_observation as obs
from . import starship_ship_sensor_observation as sensor
from . import starship_ship_sensor_control as control
from . import starship_ship_online_prediction as prediction
from . import starship_physics as env, starship_sixdof as dyn
from .starship_artifacts import write_verified_checkpoint
from .starship_replanning_executor import ReplanningExecutor
from .starship_return_feasibility import backend
from .starship_ship_fault_screen import AvailableCapacityController
from .starship_ship_return import advance_plant, control_interval, record_sample
from .starship_sixdof_mission import vehicle, _sample
from ..intelligence import starship_ship_sensor_agent as agent

CONTINUE, ADOPT, TRACK = control.CONTINUE, control.ADOPT, control.TRACK
CONFIG = {
    "application_delay_s": 600., "tracking_delay_s": 30.,
    "forecast_timeout_s": 180., "forecast_batch_timeout_s": 240.,
    "maximum_forecasts": 6, "maximum_forecast_workers": 2,
    "guard_timeout_s": 45., "minimum_apply_reserve_s": 2.,
    "contact_limits": dict(control.LIMITS),
}


def pack_sensor_control(value):
    """Share saved forecasts without changing any live Guard input or receipt.

    Shallow copies come first: copying the live log would expand every repeated
    forecast record before it could be removed. Unrepresentable failure evidence
    stays embedded under the original schema instead of being discarded.
    """
    from .starship_ship_online_guard import BUNDLE_KEYS
    log = dict(value)
    if log.get("schema") != "missionos.starship_m3_sensor_control.v1":
        raise ValueError("sensor_storage_input_schema")
    try:
        if "controller_audit" in log:
            if log["controller_audit"] != log.get("record", {}).get("control_capability_audit"):
                raise ValueError("sensor_storage_audit_not_shared")
            log.pop("controller_audit")  # The same audit remains inside record.
        forecasts, rows = log["forecasts"], log["guard_checks"]
        if type(forecasts) is not list or len(forecasts) not in (0, 6) or type(rows) is not list:
            raise ValueError("sensor_storage_incomplete_forecast_table")
        refs = [obs.digest(row) for row in forecasts]
        compact = []
        for row in rows:
            if row.get("kind") == "tracking_precondition":
                if set(row) != {"kind", "receipt"}:
                    raise ValueError("sensor_storage_tracking_row_shape")
                compact.append(dict(row))
                continue
            if row.get("kind") != "separate_process" or set(row) != {"kind", "bundle", "receipt"}:
                raise ValueError("sensor_storage_guard_row_shape")
            bundle = row["bundle"]
            if (type(bundle) is not dict or set(bundle) != BUNDLE_KEYS or len(forecasts) != 6
                    or bundle["forecasts"] != forecasts):
                raise ValueError("sensor_storage_guard_forecasts_not_shared")
            compact.append({**row, "bundle_sha256": obs.digest(bundle),
                "bundle": {**{k: v for k, v in bundle.items() if k != "forecasts"},
                           "forecast_refs": list(refs)}})
        log["schema"] = "missionos.starship_m3_sensor_control.v2"
        log["guard_checks"] = compact
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        log["storage_fallback"] = str(error)
    return log


def validate_config(value):
    if (type(value) is not dict or set(value) != set(CONFIG)|{"physical_factor", "accelerometer_gain"}
            or any(value[key] != expected for key, expected in CONFIG.items())
            or (value["physical_factor"], value["accelerometer_gain"]) not in ((1., 1.), (.8, 1.), (1., .8))):
        raise ValueError("unsupported_m3_sensor_configuration")
    return deepcopy(value)


def build_request(run_id, sequence, state, nominal, current, application, *, tracking=None,
                  forecasts=None, feedback=None):
    """Only arrived measurements and computed summaries cross the model boundary."""
    from .starship_ship_online_guard import numeric_evidence
    observed = obs.observation(state, nominal, "complete_burn")
    observed["tracking_requested"] = tracking is not None
    if forecasts is None:
        preview = {"status": "not_requested", "scope": prediction.SCOPE}
        evidence = {"accel_factor": current["factor_estimate"],
            "accel_band_low": current["band"][0], "accel_band_high": current["band"][1],
            "accel_samples": current["accepted_samples"], "tracking_delay_s": 30.,
            "application_time_s": application}
    else:
        preview = prediction.forecast_preview(forecasts)
        evidence = numeric_evidence(current, tracking, application, forecasts)
    return agent.validate_request({"schema": agent.OBSERVATION_SCHEMA_V3,
        "request_id": f"{run_id}-sensor-{sequence}", "issued_at_s": state.time_s,
        "deadline_s": application-2., "observation": observed,
        "accelerometer_estimate": deepcopy(current), "tracking_observation": deepcopy(tracking),
        "previews": preview, "evidence": evidence, "guard_feedback": deepcopy(feedback)})


def rejected_feedback(request, decision, receipt):
    """Forward a bound rejection, not a recommendation or synthesized model answer."""
    if (receipt.get("passed") is not False or receipt.get("request_sha256") != agent.digest(request)
            or receipt.get("selected_action") != decision.get("accepted_action")
            or decision.get("request_sha256") != agent.digest(request)
            or receipt.get("receipt_id") != obs.digest({k: v for k, v in receipt.items() if k != "receipt_id"})
            or decision.get("decision_id") != obs.digest({k: v for k, v in decision.items() if k != "decision_id"})):
        raise ValueError("sensor_reconsideration_not_bound")
    return agent.validate_guard_feedback({"schema": agent.GUARD_FEEDBACK_SCHEMA,
        "rejected_action": decision["accepted_action"], "prior_request_sha256": agent.digest(request),
        "prior_decision_id": decision["decision_id"], "receipt_id": receipt["receipt_id"],
        "failed_checks": [k for k, value in receipt["checks"].items() if value is False],
        "reconsideration": 1, "scope": prediction.SCOPE}, scope=prediction.SCOPE)


class SymptomReplanningExecutor(ReplanningExecutor):
    def __init__(self, *args, sensor_config, decide, forecast_map, capture, guard_check,
                 deadline, mode, **kwargs):
        self.sensor_config = self.validate_sensor_config(sensor_config)
        self.sensor_decide, self.forecast_map = decide, forecast_map
        self.capture, self.guard_check = capture, guard_check
        self.deadline, self.sensor_mode = deadline, mode
        self._cap = None
        self._managing = self._observed = False
        self._sensor_steps, self._sensor_started = 0, None
        self._sensor_samples, self._sensor_events = [], []
        self._force_rows, self._tracking_rows = [], []
        self._complete_time = self._application_time = self._cutoff = None
        self._authority = self._snapshot = self._request_snapshot = self._delivery = self._receipt = None
        self._telemetry = self._prepared = None
        self._consumed, self._selection = set(), None
        self._return_initial = self._return_history = None
        self._sequence = 0
        self.sensor_log = {"schema": "missionos.starship_m3_sensor_control.v1",
            "milestones": [], "requests": [], "decisions": [], "forecasts": [],
            "guard_checks": [], "uses": [], "fallback": [], "error": None,
            "complete": False, "ai_control_completed": False,
            "verification_scope": prediction.SCOPE}
        super().__init__(*args, **kwargs)
        if self.retained_count != 0 or mode not in ("fixture", "live"):
            raise ValueError("m3_sensor_requires_zero_retained_new_cohort")

    def _mark(self, name, **data):
        row = {"time_s": self.s.time_s, "event": name, **data}
        self.sensor_log["milestones"].append(row)
        self.event(name, **data)

    def _attach(self):
        before = obs.digest(asdict(self.s))
        history = self.controller.to_dict()
        self._nominal = vehicle(self.p, payload_count=0)
        self.v = self.make_plant(self._nominal)
        model = obs.model_vehicle(self._nominal, 1.)
        self._cap = obs.SwitchingController(self._nominal, model, self.p,
            self.controller.return_time_s, self.controller.released_count,
            obs.SwitchAudit(self._nominal, model, self.v))
        self._cap.controller = AvailableCapacityController.from_dict(history)
        self.controller = self._cap.controller
        self._return_initial, self._return_history = asdict(self.s), deepcopy(history)
        self.samples.append(_sample(self.s, self.v, self.controller.phase))
        self._sensor_next_sample = self.s.time_s
        self._sensor_started = time.monotonic()
        if obs.digest(asdict(self.s)) != before or self.controller.to_dict() != history:
            raise ValueError("sensor_attachment_changed_state_or_controller_history")
        self._mark("m3_sensor_attached", state_sha256=before,
                   controller_sha256=obs.digest(history), handoff_sha256=self.deployment_binding["handoff_sha256"])

    def make_plant(self, nominal):
        """Legacy experiment harness; compositions may supply an audited injector."""
        return obs.model_vehicle(nominal, self.sensor_config["physical_factor"])

    def validate_sensor_config(self, value):
        return validate_config(value)

    def accelerometer_gain(self):
        return self.sensor_config["accelerometer_gain"]

    def _event(self, name, detail="", **data):
        self._sensor_events.append({"time_s": self.s.time_s, "event": name, "detail": detail, **data})
        self.event(name, detail, **data)
        if name == "deorbit_cutoff_command" and self._cutoff is None:
            self._cutoff = self.s.time_s

    def step(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("m3_sensor_execution_wall_budget")
        if self._cap is None:
            if self.dispatch is None:
                return super().step()
            self._attach()
        if (not self._observed and not self._managing and self._cutoff is not None
                and self.s.time_s >= self._cutoff+2.-1e-8):
            self._observed, self._managing = True, True
            try:
                self._diagnose()
            except Exception as error:
                self.sensor_log["error"] = {"type": type(error).__name__, "detail": str(error)}
                self._mark("m3_sensor_unresolved", reason=type(error).__name__)
            finally:
                self._managing = False
        self._apply_if_due()
        o = dyn.observe(self.s, self.v)
        step = self._cap.update(self.s, self._sensor_samples, self._event)
        self.s = step.state
        if step.transition_only:
            return
        command, diagnostic = self._cap.command(step, o["altitude_m"])
        self._sensor_next_sample = record_sample(self.s, self.v, self.controller.phase,
            command, diagnostic, self._sensor_samples, self._sensor_next_sample, self.p)
        self.next_sample = record_sample(self.s, self.v, self.controller.phase,
            command, diagnostic, self.samples, self.next_sample, self.p)
        if env.norm(self.s.omega_body_rad_s) > 5:
            raise ValueError("angular_rate_envelope_exceeded")
        if self.controller.phase == "deorbit_burn":
            self._force_rows.append(obs.specific_force_row(self.s, self.v, self.controller.phase))
            self._tracking_rows.append(sensor.tracking_source_row(self.s, self.controller.phase))
        self._cap.audit.inspect("plant", self.s, self.v)
        dt = control_interval(self.p, step.throttle, o["altitude_m"])
        if self._application_time is not None and self.s.time_s < self._application_time-1e-9:
            dt = min(dt, self._application_time-self.s.time_s)
        self.s, contact = advance_plant(self.s, self.v, command,
            dt,
            self.p["geometry"]["ship_length_m"], self.p["geometry"]["radius_m"],
            o["altitude_m"], o["air_speed_mps"])
        self._sensor_steps += 1
        if contact and contact["contact"]:
            self.contact = contact
            self.termination = "low_speed_surface_contact" if contact["surface_relative_speed_mps"] <= 5 else "surface_impact"
            self._event(self.termination)

    def _await(self, label, call, timeout, *, reserve=2., cancellable=False):
        """Only I/O/orchestration goes in this thread; forecasts use processes."""
        started = time.monotonic()
        remaining = min(timeout, self.deadline-started,
                        self._application_time-self.s.time_s-reserve)
        # The owned forecast processes have shared two-second TERM and KILL
        # waits. Reserve their cleanup inside, never after, this operation's
        # existing wall/application limits.
        cleanup_reserve = 5. if cancellable else 0.
        work_remaining = remaining-cleanup_reserve
        if work_remaining <= 0:
            raise TimeoutError("sensor_operation_no_headroom")
        cancel = Event() if cancellable else None
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(lambda: call(deadline=started+work_remaining, cancel=cancel)) if cancellable else pool.submit(call)
        try:
            value = self.wait("m3_sensor_"+label,
                lambda: ("ready", future.result()) if future.done() else None,
                work_remaining, cutoff_time_s=self._application_time-reserve-cleanup_reserve)
            if value is None:
                raise TimeoutError("sensor_"+label+"_deadline")
            return value[1]
        finally:
            if cancel is not None:
                cancel.set()
            future.cancel()
            try:
                if cancellable and not future.done():
                    value = self.wait("m3_sensor_"+label+"_cleanup",
                        lambda: ("stopped",) if future.done() else None,
                        max(0., started+remaining-time.monotonic()),
                        cutoff_time_s=self._application_time-reserve)
                    if value is None:
                        raise TimeoutError("sensor_"+label+"_cleanup_deadline")
            finally:
                # Even an error while stepping must not let finalization race
                # the forecast callback's output writes or process cleanup.
                pool.shutdown(wait=cancellable, cancel_futures=True)

    def _current(self):
        return obs.estimate(self._prepared, self.s.time_s)

    def runtime_sources(self):
        from .starship_m3_sensor_mission import source_hashes
        return source_hashes()

    def _request(self, *, forecasts=None, feedback=None):
        self._sequence += 1
        state = self.s
        if forecasts is not None:
            self._request_snapshot = prediction.make_snapshot(self.s, self._cap, self.p, run_id=self.run_id,
                handoff_sha256=self.deployment_binding["handoff_sha256"], retained_count=0,
                cutoff_time_s=self._cutoff, complete_time_s=self._complete_time, application_time_s=self._application_time)
            state = dyn.state_from_dict(self._request_snapshot["state"])
            self._authority = self._await("request_capture", lambda: self.capture({"snapshot": self._snapshot,
                "request_snapshot": self._request_snapshot, "tracking_delivery": self._delivery,
                "source_sha256": self._authority["source_sha256"], "backend": backend()}), 10., reserve=47.)
        tracking = (sensor.estimate_tracking(self._delivery, self._nominal, state.time_s)
                    if self._delivery is not None else None)
        current = obs.estimate(self._prepared, state.time_s)
        def assemble():
            return build_request(self.run_id, self._sequence, state, self._nominal, current,
                self._application_time, tracking=tracking, forecasts=forecasts, feedback=feedback)
        req = (self._await("prepare_request", assemble, 45., reserve=47.)
               if forecasts is not None else assemble())
        self.sensor_log["requests"].append(req)
        return req

    def _assess(self, req):
        deadline = min(self.deadline, time.monotonic()+self._application_time-self.s.time_s-2.)
        def safe_decide(envelope):
            try:
                return self.sensor_decide(envelope)
            except (OSError, TimeoutError, ValueError) as error:
                return {"status": "transport_error", "model_inference_invoked": None,
                        "error_type": type(error).__name__}
        result = self._await("judgment", lambda: control.assess(req, self.run_id,
            obs.digest(self.sensor_config), self._application_time, safe_decide,
            allow_fixture=self.sensor_mode == "fixture", deadline=deadline), 185., reserve=47.)
        action, rows, envelopes = result
        self.sensor_log["decisions"].extend(rows)
        self.sensor_log.setdefault("envelopes", []).extend(envelopes)
        return action, rows[-1] if rows else None

    def _tracking_required(self, req, action):
        receipt = {"schema": "missionos.ship_sensor_tracking_precondition.v1", "passed": False,
            "request_sha256": agent.digest(req), "selected_action": action,
            "checks": {"requested_independent_tracking_available": False}, "scope": prediction.SCOPE}
        receipt["receipt_id"] = obs.digest(receipt)
        self.sensor_log["guard_checks"].append({"kind": "tracking_precondition", "receipt": receipt})
        return receipt

    def _diagnose(self):
        self._complete_time = self.s.time_s
        self._application_time = self._complete_time+self.sensor_config["application_delay_s"]
        # Identical noise stream across cases; the fault identity does not select it.
        self._telemetry = sensor.measure_accelerometer(self._force_rows, 0, 0,
            gain=self.accelerometer_gain())
        self._prepared = obs.prepare_telemetry(self._telemetry, self._nominal)
        current = self._current()
        if current["accepted_samples"] < obs.ESTIMATOR["minimum_samples"] or current["band"] is None:
            raise ValueError("m3_sensor_burn_measurement_insufficient")
        self.sensor_log["initial_estimate"] = deepcopy(current)
        self._mark("m3_sensor_burn_observed", estimate_id=current["estimate_id"], anomaly=current["anomaly"])
        if not current["anomaly"]:
            self.sensor_log["complete"] = True
            self._mark("m3_sensor_normal_monitoring", intervention=False)
            return
        req = self._request()
        action, decision = self._assess(req)
        if action in (CONTINUE, ADOPT):
            receipt = self._tracking_required(req, action)
            req = self._request(feedback=rejected_feedback(req, decision, receipt))
            action, decision = self._assess(req)
        if action != TRACK:
            if action in (CONTINUE, ADOPT):
                self._tracking_required(req, action)
            self.sensor_log["fallback"].append({"time_s": self.s.time_s, "action": TRACK,
                "reason": "no_admissible_observation_request", "basis": "preapproved_observation_only"})
        self._mark("m3_sensor_tracking_requested", basis="ai" if action == TRACK and self.sensor_mode == "live" else
                   "fixture" if action == TRACK else "checked_fallback")
        measured = sensor.measure_tracking(self._tracking_rows, 0)
        self._delivery = sensor.request_tracking(measured, self.s.time_s, obs.digest(self._tracking_rows))
        while self.s.time_s < self._delivery["received_at_s"]-1e-8:
            self.step()
        tracking = sensor.estimate_tracking(self._delivery, self._nominal, self.s.time_s)
        self._mark("m3_sensor_tracking_received", delivery_id=self._delivery["delivery_id"])
        self._snapshot = prediction.make_snapshot(self.s, self._cap, self.p, run_id=self.run_id,
            handoff_sha256=self.deployment_binding["handoff_sha256"], retained_count=0,
            cutoff_time_s=self._cutoff, complete_time_s=self._complete_time, application_time_s=self._application_time)
        sources = self.runtime_sources()
        # This transport is write-once at the host, before any candidate is generated.
        self._authority = self._await("capture", lambda: self.capture({"snapshot": self._snapshot,
            "request_snapshot": self._snapshot, "tracking_delivery": self._delivery,
            "source_sha256": sources, "backend": backend()}), 10., reserve=287.)
        current = obs.estimate(self._prepared, self._snapshot["state"]["time_s"])
        points = [tracking["band"][0], tracking["factor_estimate"], tracking["band"][1]]
        jobs = [{"snapshot": self._snapshot, "action": choice, "sampled_factor": factor,
            "accelerometer_estimate": current, "source_sha256": sources, "backend": backend(),
            "timeout_s": self.sensor_config["forecast_timeout_s"]}
            for choice in (CONTINUE, ADOPT) for factor in points]
        forecasts = self._await("forecasts", lambda **limits: self.forecast_map(jobs, **limits),
            self.sensor_config["forecast_batch_timeout_s"], reserve=197., cancellable=True)
        self.sensor_log["forecasts"] = forecasts
        req = self._request(forecasts=forecasts)
        action, decision = self._assess(req)
        if action in (CONTINUE, ADOPT):
            receipt = self._check(req, action, forecasts)
            if not receipt["passed"] and decision is not None:
                req = self._request(forecasts=forecasts, feedback=rejected_feedback(req, decision, receipt))
                action, decision = self._assess(req)
                receipt = self._check(req, action, forecasts) if action in (CONTINUE, ADOPT) else None
            if receipt and receipt["passed"]:
                self._reserve(req, action, receipt, decision, "ai" if self.sensor_mode == "live" else "fixture")
                return
            # A valid but rejected choice is recorded, never renamed a successful AI decision.
        for choice in (CONTINUE, ADOPT):
            receipt = self._check(req, choice, forecasts)
            if receipt["passed"]:
                self.sensor_log["fallback"].append({"time_s": self.s.time_s, "action": choice,
                    "reason": "no_admitted_model_judgment", "basis": "same_online_guard"})
                self._reserve(req, choice, receipt, None, "checked_fallback")
                return
        self._mark("m3_sensor_unresolved", reason="no_admitted_return_assumption")

    def _check(self, req, action, forecasts):
        bundle = {"schema": "missionos.ship_sensor_online_guard_bundle.v1", "run_id": self.run_id,
            "authority_sha256": obs.digest(self._authority), "request": req,
            "accelerometer_telemetry": self._telemetry, "tracking_delivery": self._delivery,
            "snapshot": self._snapshot, "request_snapshot": self._request_snapshot,
            "forecasts": forecasts, "selected_action": action,
            "checked_at_s": self.s.time_s, "source_sha256": self._authority["source_sha256"], "backend": backend()}
        receipt = self._await("guard", lambda: self.guard_check(bundle, self._authority),
            self.sensor_config["guard_timeout_s"])
        self.sensor_log["guard_checks"].append({"kind": "separate_process", "bundle": bundle, "receipt": receipt})
        return receipt

    def _reserve(self, req, action, receipt, decision, basis):
        self._receipt = receipt
        self._selection = {"request_sha256": agent.digest(req), "selected_action": action,
            "decision_id": decision["decision_id"] if decision else None, "basis": basis}
        self._mark("m3_sensor_action_reserved", action=action, basis=basis, receipt_id=receipt["receipt_id"])

    def _apply_if_due(self):
        if self._receipt is None or self.sensor_log["uses"] or self.s.time_s < self._application_time-1e-9:
            return
        from .starship_ship_online_guard import consume_receipt
        if (not math.isclose(self.s.time_s, self._application_time, rel_tol=0., abs_tol=1e-8)
                or self.controller.phase != "ballistic_return" or self.controller.record["status"] != "active"
                or not consume_receipt(self._receipt, self._consumed, run_id=self.run_id,
                    request_sha256=self._selection["request_sha256"], authority_sha256=obs.digest(self._authority),
                    selected_action=self._selection["selected_action"], application_time_s=self._application_time,
                    snapshot_id=self._snapshot["snapshot_id"])):
            raise ValueError("m3_sensor_execution_receipt_or_time_invalid")
        before = obs.digest(asdict(self.s))
        if self._selection["selected_action"] == ADOPT:
            self._cap.apply_switch(self.s.time_s, self._receipt["factor_estimate"],
                {"kind": "online_guard_receipt", "receipt_id": self._receipt["receipt_id"]})
        if obs.digest(asdict(self.s)) != before:
            raise ValueError("m3_sensor_assumption_switch_changed_physical_state")
        use = {"time_s": self.s.time_s, **self._selection, "receipt_id": self._receipt["receipt_id"],
               "state_sha256": before, "consumed_once": True, "plant_unchanged": True,
               "assumed_factor": self._cap.switch["factor"] if self._cap.switch else 1.}
        self.sensor_log["uses"].append(use)
        self.sensor_log["complete"] = True
        self._mark("m3_sensor_action_executed", **{k: v for k, v in use.items() if k != "time_s"})

    def finish(self):
        result = super().finish()
        log = pack_sensor_control(self.sensor_log)
        log.update(complete_time_s=self._complete_time, application_time_s=self._application_time,
            cutoff_time_s=self._cutoff, capture_authority=deepcopy(self._authority), snapshot=deepcopy(self._snapshot),
            request_snapshot=deepcopy(self._request_snapshot),
            accelerometer_telemetry=deepcopy(self._telemetry), tracking_delivery=deepcopy(self._delivery),
            attachment_controller=deepcopy(self._return_history),
            fault_harness={"trigger": "accepted_return_dispatch", **self.sensor_config})
        if self._cap is not None:
            samples = self._sensor_samples+[_sample(self.s, self.v, self.controller.phase)]
            run = {"state": self.s, "samples": samples, "events": self._sensor_events,
                "termination": self.termination, "contact": self.contact, "steps": self._sensor_steps,
                "error": None}
            record = obs.record("m3_same_flight_sensor_return", self._return_initial, self.v,
                self._nominal, self._cap, run, plant_factor=self.sensor_config["physical_factor"],
                switch=None if self._cap.switch is None else {k: self._cap.switch[k] for k in ("factor", "time_s")},
                wall_time_s=time.monotonic()-self._sensor_started)
            record = prediction.json_copy(record)
            log["record"] = record
            verification = obs.verify_record(record, control.LIMITS)
            log["contact_verification"] = verification
            log["ai_control_completed"] = bool(self.sensor_mode == "live" and log["complete"]
                and verification["contact_limits_met"] and not log["fallback"] and log["uses"]
                and log["uses"][0]["basis"] == "ai"
                and any(m["event"] == "m3_sensor_tracking_requested" and m.get("basis") == "ai" for m in log["milestones"]))
        result["sensor_control"] = prediction.json_copy(log)
        write_verified_checkpoint(self.output/"sensor-control.json", result["sensor_control"])
        return result

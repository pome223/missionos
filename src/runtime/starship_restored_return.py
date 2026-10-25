"""Typed unqualified Ship-return continuation and isolated-candidate prediction.

Both modes start with all eight actual state fields from ActualRestoredOrigin.
They share the existing trimmed-state controller and finite plant. Predictions
are new integrations of copied current evidence, never execution checkpoints.
An outer governed caller still owns source authentication, process/deadline
budgets, human approval, candidate admission and dispatch. No old certificate or
MissionOS execution grant is admitted or created here.
"""
from __future__ import annotations

from dataclasses import InitVar, asdict, dataclass, field
from hashlib import sha256
import json
import math
import time

from . import starship_physics as env, starship_sixdof as dyn
from .starship_concurrent import PhysicsTick, drain
from .starship_restored_contract import ActualRestoredOrigin, RestoredCandidateSchedule
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_ship_return import ReturnController, advance_plant, control_interval, record_sample
from .starship_sixdof_mission import _attitude, _sample, control, point_state

MAXIMUM_CONTROL_TICKS = 200000
CANDIDATES = {"nominal": "nominal_s", "next_orbit": "next_orbit_s"}
INHIBIT_REASONS = frozenset({"forecast_unavailable", "model_no_valid_judgment",
    "judgment_window_unavailable",
    "candidate_inadmissible", "approval_unavailable", "reservation_confirmation_unavailable",
    "source_or_observation_changed", "host_ready_unavailable", "dispatch_not_admitted",
    "original_guard_expired", "run_budget_exhausted", "notice_blocked_return"})


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("restored_return_requires_finite_json") from error


def _read(raw):
    return json.loads(raw)


def _digest(value):
    return sha256(_encode(value)).hexdigest()


@dataclass(frozen=True)
class RestoredReturnRequest:
    origin: ActualRestoredOrigin
    schedule: RestoredCandidateSchedule
    candidate_id: str
    profile_raw_bytes: InitVar[bytes]
    expected_source_sha256: InitVar[dict]
    maximum_control_ticks: int = MAXIMUM_CONTROL_TICKS
    _request_json: bytes = field(init=False, repr=False)
    _profile_json: bytes = field(init=False, repr=False)
    _profile_raw_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self, profile_raw_bytes, expected_source_sha256):
        _require(type(self.origin) is ActualRestoredOrigin and type(self.schedule) is RestoredCandidateSchedule,
            "restored_return_requires_actual_origin_and_schedule")
        _require(type(self.candidate_id) is str and self.candidate_id in CANDIDATES,
            "restored_return_unknown_candidate")
        _require(type(self.maximum_control_ticks) is int and 1 <= self.maximum_control_ticks <= MAXIMUM_CONTROL_TICKS,
            "restored_return_control_tick_budget")
        self.origin.validate_profile(profile_raw_bytes)
        origin, schedule = self.origin.record(), self.schedule.record()
        _require(type(expected_source_sha256) is dict
            and _encode(expected_source_sha256) == _encode(origin["source_sha256"]),
            "restored_return_host_source_binding_changed")
        _require(schedule["origin_sha256"] == _digest(origin)
            and _encode(self.schedule.origin.record()) == _encode(origin),
            "restored_return_schedule_belongs_to_different_origin")
        when, end = schedule[CANDIDATES[self.candidate_id]], schedule["maximum_simulation_time_s"]
        _require(origin["state"]["time_s"] < when < end <= 16000.,
            "restored_return_candidate_or_deadline_outside_scope")
        profile = _read(profile_raw_bytes)
        request = {"schema": "missionos.starship_restored_return_request.v1", "origin_sha256": _digest(origin),
            "source_record_sha256": self.origin.source_record_sha256,
            "source_sha256": _digest(origin["source_sha256"]), "profile_sha256": origin["profile_sha256"],
            "inventory_receipt_chain_sha256": origin["inventory_receipt_chain_sha256"],
            "inventory_generation": origin["inventory_generation"], "retained_count": origin["retained_count"],
            "released_count": origin["released_count"], "schedule_sha256": _digest(schedule),
            "candidate_id": self.candidate_id, "return_time_s": when, "origin_time_s": origin["state"]["time_s"],
            "maximum_simulation_time_s": end, "maximum_control_ticks": self.maximum_control_ticks,
            "return_start_clock_policy": "first_existing_control_tick_at_or_after_candidate_time",
            "maximum_start_clock_slip_s": profile["integration"]["coast_dt_s"],
            "controller_policy": TRIMMED_POLICY_ID, "bounded_ship_flaps": True,
            "registered_return": False, "old_certificate_admitted": False,
            "qualification_refreshed": False, "mission_authority_granted": False, "dispatch_authorized": False}
        if "opportunity_anchor" in schedule:
            request["opportunity_anchor"] = schedule["opportunity_anchor"]
        object.__setattr__(self, "_request_json", _encode(request))
        object.__setattr__(self, "_profile_json", _encode(profile))
        object.__setattr__(self, "_profile_raw_bytes", profile_raw_bytes)

    def record(self):
        return _read(self._request_json)

    def profile(self):
        return _read(self._profile_json)


@dataclass(frozen=True)
class RestoredReturnResult:
    """Immutable simulation evidence; its bytes cannot initialize a request."""
    _record_json: bytes = field(repr=False)

    def __post_init__(self):
        _require(type(self._record_json) is bytes, "restored_return_result_requires_bytes")
        value = _read(self._record_json)
        _require(type(value) is dict and value.get("schema") in (
            "missionos.starship_restored_return_prediction.v1", "missionos.starship_restored_return_continuation.v1")
            and value.get("prediction_only") is (value["schema"] == "missionos.starship_restored_return_prediction.v1")
            and all(value.get(key) is False for key in ("old_certificate_admitted", "qualification_refreshed",
                "mission_authority_granted", "dispatch_authorized", "physical_execution")),
            "restored_return_result_scope_invalid")
        _encode(value)

    def record(self):
        return _read(self._record_json)

    @property
    def sha256(self):
        return sha256(self._record_json).hexdigest()


def _contact_metrics(state, contact, profile):
    if contact is None:
        return {"contact_observed": False, "contact_envelope_met": False, "gates": {}}
    up = contact["surface_normal_eci"]
    axis = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    tilt = math.degrees(math.acos(max(-1., min(1., env.dot(axis, up)))))
    speed = contact["surface_relative_speed_mps"]
    rate = env.norm(state.omega_body_rad_s)
    gates = {"nonoverlapping_descending_contact": contact["initial_overlap"] is False
        and contact["surface_normal_speed_mps"] <= 0,
        "contact_speed": speed <= 5., "tilt": tilt <= 5., "body_rate": rate <= .02,
        "reserve": state.propellant_kg >= profile["ship"]["return_reserve_kg"]}
    return {"contact_observed": True, "point_speed_mps": speed,
        "point_normal_speed_mps": contact["surface_normal_speed_mps"], "tilt_deg": tilt,
        "body_rate_rad_s": rate, "propellant_kg": state.propellant_kg,
        "point_ecef_m": list(env.eci_to_ecef(contact["point_eci_m"], contact["event_time_s"])),
        "event_time_s": contact["event_time_s"], "gates": gates, "contact_envelope_met": all(gates.values())}


def _advanced(before, after, craft, dt, receipt):
    """Reject reset-like plant responses while retaining the last finite state."""
    same_time_contact = (type(after) is dyn.State6DOF and type(receipt) is dict and receipt.get("contact") is True
        and receipt.get("initial_overlap") is True and _encode(asdict(after)) == _encode(asdict(before)))
    _require(type(after) is dyn.State6DOF and (before.time_s < after.time_s or same_time_contact)
        and after.time_s <= before.time_s+dt+1e-7
        and after.propellant_kg <= before.propellant_kg+1e-6
        and tuple(x.available for x in after.engine_states) == tuple(x.available for x in before.engine_states),
        "restored_return_plant_changed_clock_fuel_or_availability")
    dyn._validate_pair(after, craft, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), after.flap_angles_rad))


def _steps(request, *, prediction, context=None):
    _require(type(request) is RestoredReturnRequest, "restored_return_requires_typed_request_not_forecast")
    before_origin, binding, p = request.origin.record(), request.record(), request.profile()
    s, v = dyn.state_from_dict(before_origin["state"]), dyn.vehicle_from_dict(before_origin["vehicle"])
    initial_state, initial_vehicle = asdict(s), asdict(v)
    _require(_encode(initial_state) == _encode(before_origin["state"])
        and _encode(initial_vehicle) == _encode(before_origin["vehicle"]), "restored_return_deserialization_changed_actual_origin")
    phase, controller, contact, termination = "orbital_coast", None, None, "time_limit"
    return_start_state = None
    next_sample, steps, ticks = s.time_s, 0, 0
    samples, events = [], []

    def actual_snapshot():
        # This closure reads the generator's actual held plant objects. It is
        # distinct from the previously published JSON telemetry snapshot and
        # never resumes the generator, observes a forecast or assigns state.
        return {"state": asdict(s), "vehicle": asdict(v), "phase": phase,
            "binding": context["request"].record(),
            "return_controller": None if controller is None else controller.to_dict(), "completed": False}

    if context is not None:
        context["actual_snapshot_provider"] = actual_snapshot
        if context.get("final_handoff_policy") is not None:
            # Only the explicit final composition may transfer the exact held
            # objects. Default v1 actors retain their original public surface.
            context["actual_owner_provider"] = lambda: (s, v, controller)

    def event(name, detail="", **data):
        events.append({"time_s": s.time_s, "event": name, "detail": detail, **data})

    event("restored_return_continuation_start", "Exact actual state, vehicle, inventory and finite actuators inherited; no authority granted.")
    while s.time_s < binding["maximum_simulation_time_s"]-1e-9:
        if ticks >= binding["maximum_control_ticks"]:
            termination = "control_tick_limit"
            event(termination, "Explicit request budget exhausted; actual last state retained, no retry.")
            break
        ticks += 1
        if context is not None:
            binding = context["request"].record()
            context["snapshot"] = {"state": asdict(s), "vehicle": asdict(v), "phase": phase,
                "binding": binding, "return_controller": None if controller is None else controller.to_dict(),
                "completed": False}
            if phase == "orbital_coast":
                row = {"state": asdict(s), "retained_count": before_origin["retained_count"]}
                if not context["coast_history"] or _encode(context["coast_history"][-1]) != _encode(row):
                    context["coast_history"].append(row)
        yield PhysicsTick("ship", float(s.time_s), phase, s, released_count=before_origin["released_count"],
            sequencer_state="closed_restored_inventory", deployment_withheld=True)
        if context is not None:
            # A reservation may change while the actual coast tick is yielded;
            # only its binding changes. No state, actuator or controller reset.
            binding = context["request"].record()
            if context.get("stop_reason") is not None:
                _require(controller is None and phase == "orbital_coast",
                    "restored_governance_stop_after_return_started")
                termination = "return_inhibited_by_governance"
                event(termination, "Current reservation not dispatched; actual coast state retained, recovery unresolved.",
                    reason=context["stop_reason"])
                break
        observed = dyn.observe(s, v)
        altitude = observed["altitude_m"]
        if controller is None and s.time_s >= binding["return_time_s"]:
            _require(s.time_s-binding["return_time_s"] <= binding["maximum_start_clock_slip_s"]+1e-7,
                "restored_return_missed_candidate_start_deadline")
            # Explicit pre-controller witness for the independent due-tick
            # check. The ordinary sample at this time already has the new
            # deorbit phase, so it must not be interpolated as ongoing coast.
            return_start_state = asdict(s)
            controller = ReturnController(TRIMMED_POLICY_ID, binding["return_time_s"], before_origin["released_count"],
                allocation_enabled=True, registered=False)
            controller.start(s, v, p, samples, event)
            phase = controller.phase
            continue
        if controller is None:
            up, east, north = env.local_frame(point_state(s))
            radial = env.dot(s.v_eci_mps, up)
            tangent_vector = env.add(s.v_eci_mps, env.scale(up, -radial))
            tangent = env.unit(tangent_vector) if env.norm(tangent_vector) > 1. else east
            command, diagnostics = control(s, v, _attitude(tangent, north), 0., 0, p,
                control_interval_s=control_interval(p, 0., altitude))
            throttle = 0.
        else:
            step = controller.update(s, v, p, samples, event)
            _require(_encode(asdict(step.state)) == _encode(asdict(s)), "restored_return_controller_assigned_actual_state")
            s, phase = step.state, controller.phase
            if step.transition_only:
                continue
            command, diagnostics = controller.command(step, v, p, altitude)
            throttle = step.throttle
        next_sample = record_sample(s, v, phase, command, diagnostics, samples, next_sample, p)
        if env.norm(s.omega_body_rad_s) > 5.:
            termination = "angular_rate_envelope_exceeded"
            event(termination, "Unchanged 5rad/s development bound; not a structural solver.")
            break
        dt = min(binding["maximum_simulation_time_s"]-s.time_s, control_interval(p, throttle, altitude))
        try:
            endpoint, receipt = advance_plant(s, v, command, dt, p["geometry"]["ship_length_m"],
                p["geometry"]["radius_m"], altitude, observed["air_speed_mps"])
            _advanced(s, endpoint, v, dt, receipt)
            s = endpoint
            steps += 1
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5. else "low_speed_surface_contact"
                event(termination, "Finite cylinder-envelope contact; no water or structural response.", contact=receipt)
                break
        except (ValueError, OverflowError, FloatingPointError) as error:
            termination = "numerical_failure"
            event(termination, f"{type(error).__name__}: {error}; last accepted finite state retained")
            break
    final = _sample(s, v, phase)
    final["contact"] = contact is not None
    samples.append(final)
    if termination == "time_limit":
        event(termination, "Explicit bounded absolute horizon; no return success inferred.")
    _require(_encode(before_origin) == _encode(request.origin.record()), "restored_return_mutated_actual_origin")
    record = {"schema": "missionos.starship_restored_return_prediction.v1" if prediction else
        "missionos.starship_restored_return_continuation.v1", "prediction_only": prediction,
        "binding": binding, "initial_state": initial_state, "vehicle": initial_vehicle,
        "initial_binding": request.record(),
        "return_start_state": return_start_state,
        "reservation_changes": [] if context is None else context["reservation_changes"],
        "final_state": asdict(s), "final_vehicle": asdict(v), "events": events, "samples": samples,
        "return_controller": None if controller is None else controller.to_dict(),
        "outcome": {"termination": termination, "phase": phase, "integration_steps": steps, "control_ticks": ticks,
            "contact_receipt": contact, "contact_metrics": _contact_metrics(s, contact, p)},
        "payload_releases_after_origin": 0, "retained_count": before_origin["retained_count"],
        "actual_state_initialized_from_prediction": False, "source_provenance_authenticated": False,
        "old_certificate_admitted": False, "qualification_refreshed": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False,
        "independent_physical_model": False, "model_calls": 0}
    result = RestoredReturnResult(_encode(record))
    if context is not None:
        context["snapshot"] = {"state": asdict(s), "vehicle": asdict(v), "phase": phase,
            "binding": binding, "return_controller": None if controller is None else controller.to_dict(), "completed": True}
    return result


class RestoredReturnExecution:
    """Incremental actual simulator actor; it accepts no predicted checkpoint.

    Construction exposes the first actual tick without integrating. ``step_once``
    advances existing control transitions until one finite plant interval has
    elapsed (or a terminal negative occurs). ``advance_until`` can stop before a
    hard booking guard instead of shortening commands or skipping physics.
    """

    def __init__(self, request, *, origin_record_bytes=None, final_handoff_policy=None):
        _require(type(request) is RestoredReturnRequest, "restored_return_requires_typed_request_not_forecast")
        if final_handoff_policy is not None:
            from .starship_final_handoff import FinalActualReturnHandoffPolicy
            _require(type(final_handoff_policy) is FinalActualReturnHandoffPolicy,
                "restored_execution_exact_final_handoff_policy_required")
            final_handoff_policy.validate_binding(request.origin.record()["source_sha256"],
                final_handoff_policy.plan_sha256)
        self._final_handoff_policy, self._final_handoff = final_handoff_policy, None
        self._request = request
        self._origin_seed = None
        if origin_record_bytes is not None:
            _require(type(origin_record_bytes) is bytes
                and sha256(origin_record_bytes).hexdigest() == request.origin.source_record_sha256,
                "restored_execution_actual_seed_sha256_mismatch")
            self._origin_seed = _read(origin_record_bytes)
        self._context = {"request": request, "reservation_changes": [], "coast_history": [],
            "snapshot": None, "stop_reason": None}
        if final_handoff_policy is not None:
            self._context["final_handoff_policy"] = final_handoff_policy
        self._observation_reads = 0
        self._iterator = _steps(request, prediction=False, context=self._context)
        self._result = None
        self._pull()

    def _pull(self):
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        try:
            return next(self._iterator)
        except StopIteration as completed:
            self._result = completed.value
            return None

    def snapshot(self):
        return _read(_encode(self._context["snapshot"]))

    def reacquire_actual_snapshot(self, *, clock=time.monotonic):
        """Read current yielded plant state anew without a control/plant tick.

        The read is from the generator's live state/vehicle closure, not the
        coordinator's old observation or a prediction. A different published
        snapshot is an integrity failure, not permission to reset it. The wall
        epoch is the start of this actual read, so copying/validation time still
        consumes the unchanged freshness budget.
        """
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        provider = self._context.get("actual_snapshot_provider")
        _require(self._result is None and self._context["stop_reason"] is None
            and callable(provider) and callable(clock), "restored_actual_observation_not_available")
        started = clock()
        _require(type(started) in (int, float) and math.isfinite(started), "restored_actual_observation_invalid_clock")
        actual = _read(_encode(provider()))
        _require(_encode(actual) == _encode(self.snapshot()) and actual.get("completed") is False,
            "restored_actual_observation_published_state_changed")
        completed = clock()
        _require(type(completed) in (int, float) and math.isfinite(completed) and completed >= started,
            "restored_actual_observation_invalid_clock")
        self._observation_reads += 1
        receipt = {"schema": "missionos.starship_restored_actual_observation_read.v1",
            "read_sequence": self._observation_reads, "provider": "current_yielded_plant_state",
            "state_sha256": _digest(actual["state"]), "vehicle_sha256": _digest(actual["vehicle"]),
            "binding_sha256": _digest(actual["binding"]), "simulation_time_s": actual["state"]["time_s"],
            "wall_time_s": started, "read_completed_wall_s": completed,
            "plant_advanced": False, "actual_state_assigned": False, "forecast_state_used": False}
        return actual, receipt

    @property
    def result(self):
        return self._result

    def step_once(self):
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        if self._result is not None:
            return self.snapshot()
        before = self.snapshot()["state"]["time_s"]
        while self._result is None:
            self._pull()
            if self.snapshot()["state"]["time_s"] > before:
                break
        return self.snapshot()

    def advance_until(self, time_s, *, not_after_s=None):
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        current = self.snapshot()["state"]["time_s"]
        horizon = self._context["request"].record()["maximum_simulation_time_s"]
        _require(type(time_s) in (int, float) and math.isfinite(time_s) and current <= time_s <= horizon,
            "restored_execution_invalid_advance_deadline")
        if not_after_s is not None:
            _require(type(not_after_s) in (int, float) and math.isfinite(not_after_s)
                and current <= time_s <= not_after_s <= horizon, "restored_execution_invalid_hard_guard")
        # An intentionally conservative bound avoids polling guidance or
        # changing its cadence merely to fit an approval/tool wait boundary.
        bound = max(self._request.profile()["integration"][key] for key in ("powered_dt_s", "coast_dt_s"))
        guarded = False
        while self._result is None and self.snapshot()["state"]["time_s"] < time_s:
            if not_after_s is not None and self.snapshot()["state"]["time_s"]+bound > not_after_s+1e-9:
                guarded = True
                break
            self.step_once()
        return {"snapshot": self.snapshot(), "hard_guard_stopped_before_next_step": guarded,
            "maximum_existing_step_s": bound, "steps_skipped": 0, "state_projected": False}

    def replace_reservation(self, request):
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        _require(type(request) is RestoredReturnRequest, "restored_reservation_requires_typed_actual_request")
        snapshot, previous = self.snapshot(), self._context["request"].record()
        actual, binding = request.origin.record(), request.record()
        _require(self._result is None and snapshot["phase"] == "orbital_coast"
            and snapshot["return_controller"] is None and snapshot["state"]["time_s"] < previous["return_time_s"]
            and _encode(actual["state"]) == _encode(snapshot["state"])
            and _encode(actual["vehicle"]) == _encode(snapshot["vehicle"])
            and all(binding[key] == previous[key] for key in ("source_sha256", "profile_sha256",
                "inventory_receipt_chain_sha256", "inventory_generation", "retained_count", "released_count",
                "maximum_control_ticks", "maximum_simulation_time_s"))
            and _encode(request.schedule.opportunity_anchor_record()) == _encode(self._request.schedule.opportunity_anchor_record())
            and all(request.schedule.record()[key] == self._request.schedule.record()[key]
                for key in ("nominal_s", "next_orbit_s"))
            and snapshot["state"]["time_s"] < binding["return_time_s"],
            "restored_reservation_stale_actual_state_inventory_source_or_deadline")
        self._context["reservation_changes"].append({"time_s": snapshot["state"]["time_s"],
            "state": snapshot["state"], "from_binding": previous, "to_binding": binding,
            "actual_state_assigned": False, "dispatch_authorized": False})
        self._context["request"] = request
        self._context["snapshot"]["binding"] = binding

    def stop_before_dispatch(self, reason):
        """Retain a governed unresolved negative without resuming into ignition.

        This stops a simulation trial, not the physical vehicle or its clock.
        It neither selects an alternative nor establishes a safe recovery.
        """
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        snapshot = self.snapshot()
        _require(type(reason) is str and reason in INHIBIT_REASONS
            and self._result is None and snapshot["phase"] == "orbital_coast"
            and snapshot["return_controller"] is None and self._context["stop_reason"] is None,
            "restored_governance_stop_requires_unstarted_actual_coast")
        self._context["stop_reason"] = reason
        self._pull()
        _require(self._result is not None and self.snapshot()["state"] == snapshot["state"],
            "restored_governance_stop_advanced_or_assigned_state")
        return self._result

    def final_refusal_owner_record(self, policy):
        """Describe an actual stopped pre-burn owner; no transfer or contact.

        The opt-in final caller needs the original coast/history preimages to
        verify an honored refusal. A terminal simulation result is not a
        completed flight or an alternate recovery, and cannot restart this
        actor or initialize another execution.
        """
        from .starship_final_handoff import FinalActualReturnHandoffPolicy
        _require(type(policy) is FinalActualReturnHandoffPolicy
            and policy is self._final_handoff_policy and self._final_handoff is None
            and self._result is not None and self._context["stop_reason"] in INHIBIT_REASONS,
            "restored_final_refusal_requires_bound_stopped_actual_owner")
        result, snapshot = self._result.record(), self.snapshot()
        _require(result["return_start_state"] is None and result["return_controller"] is None
            and result["outcome"]["contact_receipt"] is None and snapshot["phase"] == "orbital_coast",
            "restored_final_refusal_cannot_claim_ignition_or_contact")
        return _read(_encode({"schema": "missionos.starship_final_actual_refusal_owner.v1",
            "initial_binding": self._request.record(), "binding": self._context["request"].record(),
            "reservation_changes": self._context["reservation_changes"],
            "coast_history": self._context["coast_history"], "actual_snapshot": snapshot,
            "continuation": result, "completed_v1_flight": False, "contact": None,
            "new_execution_authority_granted": False}))

    def current_origin(self):
        _require(self._final_handoff is None, "restored_execution_permanently_handed_off")
        snapshot = self.snapshot()
        _require(self._origin_seed is not None and self._result is None and snapshot["phase"] == "orbital_coast"
            and snapshot["return_controller"] is None, "restored_actual_origin_seed_or_coast_missing")
        row = _read(_encode(self._origin_seed))
        row["final_state"], row["final_vehicle"] = snapshot["state"], snapshot["vehicle"]
        row["inventory"]["observed_time_s"] = snapshot["state"]["time_s"]
        for witness in self._context["coast_history"]:
            if _encode(row["state_history"][-1]) != _encode(witness):
                row["state_history"].append(witness)
        raw = _encode(row)
        return ActualRestoredOrigin(sha256(raw).hexdigest(), raw, self._request._profile_raw_bytes,
            self._request.origin.record()["source_sha256"])

    def handoff_actual_return(self, policy, *, dispatch):
        """Seal the current yielded plant once; never produce a v1 completion."""
        from .starship_final_handoff import _capture_actual_owner
        _require(self._final_handoff is None and policy is self._final_handoff_policy and policy is not None
            and self._result is None and self._context["stop_reason"] is None,
            "restored_execution_final_handoff_not_opted_in_or_already_used")
        # Re-read the live closure, rather than trusting a previously published
        # JSON snapshot. An altered cache cannot supply ownership.
        self.reacquire_actual_snapshot()
        handoff = _capture_actual_owner(policy=policy, request=self._context["request"],
            actual_provider=self._context.get("actual_owner_provider"),
            published_snapshot=self.snapshot(), dispatch=dispatch, coast_history=self._context["coast_history"],
            initial_binding=self._request.record(), reservation_changes=self._context["reservation_changes"])
        self._final_handoff = handoff
        self._iterator.close()
        self._context["actual_owner_provider"] = self._context["actual_snapshot_provider"] = None
        return handoff


def continue_restored_return_steps(request):
    """Yield actual continuation ticks; caller must separately authorize use."""
    return _steps(request, prediction=False)


def continue_restored_return(request, *, on_tick=None):
    """Consume a simulation continuation; accepts no forecast-result state."""
    _require(on_tick is None or callable(on_tick), "restored_return_invalid_tick_observer")
    iterator = continue_restored_return_steps(request)
    if on_tick is None:
        return drain(iterator)
    while True:
        try:
            tick = next(iterator)
        except StopIteration as completed:
            return completed.value
        on_tick(tick)


def forecast_restored_return(request, *, on_tick=None):
    """Integrate one copied-current-state candidate; never grant dispatch."""
    _require(on_tick is None or callable(on_tick), "restored_return_invalid_tick_observer")
    iterator = _steps(request, prediction=True)
    if on_tick is None:
        return drain(iterator)
    while True:
        try:
            tick = next(iterator)
        except StopIteration as completed:
            return completed.value
        on_tick(tick)


def forecast_candidate_manifest(result):
    """Compact numerical candidate evidence for an outer authority gate."""
    _require(type(result) is RestoredReturnResult, "restored_manifest_requires_typed_prediction_result")
    record = result.record()
    _require(record["prediction_only"] is True, "restored_manifest_cannot_borrow_actual_continuation")
    binding = record["binding"]
    manifest = {"schema": "missionos.starship_restored_forecast_candidate.v1",
        **{key: binding[key] for key in ("origin_sha256", "source_record_sha256", "source_sha256", "profile_sha256",
            "inventory_receipt_chain_sha256", "inventory_generation", "retained_count", "candidate_id", "return_time_s",
            "schedule_sha256", "maximum_simulation_time_s")}, "prediction_record_sha256": result.sha256,
        "prediction_only": True, "actual_state_initialized_from_prediction": False,
        "old_certificate_admitted": False, "qualification_refreshed": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False}
    if "opportunity_anchor" in binding:
        manifest["opportunity_anchor"] = binding["opportunity_anchor"]
    return manifest

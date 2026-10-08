"""Shared Ship return controller and finite stepping used by flight and prediction.

This is execution logic, not an independent verifier or a grant of authority.
The flight loop must perform admission before starting return. A forecast only
copies state and runs the same controller; it cannot dispatch a command.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
import json
import math

from . import starship_physics as env, starship_sixdof as dyn
from .starship_sixdof_contact import find_contact, hull_clearance
from .starship_retained_return import POLICY_ID, CONTINUOUS_POLICY_ID, CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID, new_record, terminal_budget
from .starship_attitude_reference import ParallelTransportFrame, ConditionedGeographicFrame


@dataclass
class GuidanceStep:
    state: dyn.State6DOF
    target: tuple
    throttle: float
    count: int
    transition_only: bool = False
    reference_diagnostics: dict | None = None


@dataclass
class ReturnController:
    policy: str
    return_time_s: float
    released_count: int
    allocation_enabled: bool = False
    registered: bool = False
    terminal_engine_out: bool = False
    failed_engine: bool = False
    phase: str = "orbital_coast"
    record: dict | None = None
    previous_budget: dict | None = None
    entry_prepared: dict | None = None
    next_trim_attempt_s: float = 0.
    trim_attempts: int = 0
    landing_frame_started: bool = False
    return_frame: object = field(default=None, repr=False)

    def __post_init__(self):
        template = new_record(self.policy)
        if self.record is None:
            self.record = template
        if (type(self.return_time_s) not in (int, float) or not math.isfinite(self.return_time_s)
                or type(self.released_count) is not int or self.released_count < 0
                or any(type(getattr(self, name)) is not bool for name in
                       ("allocation_enabled", "registered", "terminal_engine_out", "failed_engine", "landing_frame_started"))
                or type(self.trim_attempts) is not int or not 0 <= self.trim_attempts <= 3
                or not math.isfinite(self.next_trim_attempt_s)
                or not isinstance(self.record, dict) or self.record.get("policy_id") != self.policy
                or self.phase not in ("orbital_coast", "deorbit_slew", "deorbit_burn", "ballistic_return", "landing_burn")):
            raise ValueError("invalid_return_controller_context")

    def start(self, s, v, p, samples, event):
        from .starship_sixdof_mission import _sample
        if self.phase != "orbital_coast" or s.time_s < self.return_time_s:
            raise ValueError("return_not_due_or_already_started")
        if self.policy in (POLICY_ID, CONTINUOUS_POLICY_ID, CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID) and (
                self.released_count < p["payload"]["count"] or self.policy == TRIMMED_POLICY_ID):
            observed = _sample(s, v, self.phase)
            observed["retained_return"] = "activation"
            samples.append(observed)
            self.record.update(status="active", activation={"time_s": s.time_s,
                "payload_retained_count": p["payload"]["count"]-self.released_count,
                "payload_retained_mass_kg": (p["payload"]["count"]-self.released_count)*p["payload"]["mass_each_kg"],
                "state": observed})
            event("retained_return_activated", "preapproved deterministic local guidance; no safe-return guarantee",
                  policy_id=self.policy)
        self.phase = "deorbit_slew"
        event("return_requested", "configured mission timing; no fitted observed return time")

    def update(self, s, v, p, samples, event):
        from .starship_sixdof_mission import point_state, _attitude, _sample
        g = p["guidance"]
        ps, o = point_state(s), dyn.observe(s, v)
        altitude = o["altitude_m"]
        up, east, north = env.local_frame(ps)
        radial = env.dot(s.v_eci_mps, up)
        tangent_vec = env.add(s.v_eci_mps, env.scale(up, -radial))
        tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
        orbit = env.orbital_elements(ps)
        target, throttle, count = _attitude(tangent, north), 0., 0
        reference_diagnostics = None
        if self.phase == "orbital_coast" and s.time_s >= self.return_time_s:
            self.start(s, v, p, samples, event)
            return GuidanceStep(s, target, throttle, count, True)
        if self.phase in ("deorbit_slew", "deorbit_burn"):
            target = _attitude(env.scale(tangent, -1), north)
            axis = dyn.rotate(s.q_body_to_eci, (0., 0., 1.))
            if env.dot(axis, env.scale(tangent, -1)) > math.cos(math.radians(g["deorbit_max_alignment_deg"])):
                if self.phase == "deorbit_slew":
                    self.phase = "deorbit_burn"
                    event("deorbit_ignition_command", "attitude error below 5 degrees")
                throttle, count = g["deorbit_throttle"], 3
            if orbit["perigee_altitude_m"] <= g["deorbit_perigee_m"] or s.propellant_kg <= p["ship"]["return_reserve_kg"]:
                self.phase = "ballistic_return"
                event("deorbit_cutoff_command", "measured perigee or return reserve threshold")
                return GuidanceStep(s, target, throttle, count, True, reference_diagnostics)
        elif self.phase == "ballistic_return":
            # 70 degrees between +Z and actual air-relative velocity; a target
            # along the tangent would be nose-first, not broadside entry.
            flow = env.unit(env.air_relative_velocity(ps))
            lift_up = env.add(up, env.scale(flow, -env.dot(up, flow)))
            lift_up = env.unit(lift_up) if env.norm(lift_up) > 1e-8 else north
            entry_axis = env.add(env.scale(flow, math.cos(math.radians(g["entry_alpha_deg"]))), env.scale(lift_up, math.sin(math.radians(g["entry_alpha_deg"]))))
            target = _attitude(entry_axis, env.scale(north, -1))
            if self.policy == TRIMMED_POLICY_ID and self.record["status"] == "active":
                from .starship_entry_trim import prepare_entry_trim, entry_preferred, MAXIMUM_ROLL_RATE_RAD_S
                if (self.entry_prepared is None and self.trim_attempts < 3 and s.time_s >= self.next_trim_attempt_s and o["dynamic_pressure_pa"] > 1e-12):
                    actual_axis = dyn.rotate(s.q_body_to_eci, (0., 0., 1.))
                    if env.dot(actual_axis, entry_axis) > math.cos(math.radians(5.)) and env.norm(s.omega_body_rad_s) < .02:
                        prepared = prepare_entry_trim(s, v, entry_axis, flow, target)
                        self.trim_attempts += 1
                        self.next_trim_attempt_s = s.time_s+30.
                        if prepared["status"] == "prepared":
                            self.entry_prepared = prepared
                            witness = _sample(s, v, self.phase)
                            witness["entry_trim"] = "preparation"
                            samples.append(witness)
                            prepared["state"] = witness
                            self.record["entry_trim"] = prepared
                            event("entry_trim_prepared", "static prediction; actual bounded roll and surface motion still required")
                preferred, reference = entry_preferred(entry_axis, flow, target, self.entry_prepared or {})
                if self.return_frame is None:
                    self.return_frame = ConditionedGeographicFrame(target, s.time_s,
                        maximum_roll_rate_rad_s=MAXIMUM_ROLL_RATE_RAD_S)
                target = self.return_frame.target(entry_axis, reference, preferred, time_s=s.time_s, force_bridge=True)
                reference_diagnostics = {**self.return_frame.diagnostics, "basis": (self.entry_prepared or {}).get("basis", "legacy_pending_trim")}
            elif self.policy == CONTINUOUS_POLICY_ID and self.record["status"] == "active":
                # Select roll once, then parallel-transport the target frame.
                # The actual body remains governed by finite moments/actuators.
                if self.return_frame is None:
                    self.return_frame = ParallelTransportFrame(target)
                target = self.return_frame.target(entry_axis)
                reference_diagnostics = self.return_frame.diagnostics
            elif self.policy == CONDITIONED_POLICY_ID and self.record["status"] == "active":
                if self.return_frame is None:
                    self.return_frame = ConditionedGeographicFrame(target, s.time_s,
                        maximum_roll_rate_rad_s=g["max_angular_acceleration_rad_s2"]/g["attitude_frequency_rad_s"])
                target = self.return_frame.target(entry_axis, env.scale(north, -1), target, time_s=s.time_s)
                reference_diagnostics = self.return_frame.diagnostics
            flip_due = altitude < g["flip_altitude_m"] and radial < 0
            if self.record["status"] == "active":
                observed = _sample(s, v, self.phase)
                budget = terminal_budget(observed, p)
                self.record["evaluation_count"] += 1
                flip_due = budget["trigger"]
                if flip_due:
                    if self.terminal_engine_out and not self.failed_engine:
                        states = list(s.engine_states)
                        states[0] = replace(states[0], available=False, throttle=0.)
                        s, self.failed_engine = replace(s, engine_states=tuple(states)), True
                        event("terminal_engine_fault", "explicit synthetic loss of one landing engine after deorbit", engine_index=0)
                        observed = _sample(s, v, self.phase)
                        budget = terminal_budget(observed, p)
                    if self.registered and self.policy == TRIMMED_POLICY_ID:
                        from .starship_return_feasibility import certificate_ready, terminal_receipt
                        certified, _ = certificate_ready(p)
                        receipt = terminal_receipt(observed, budget, certified)
                        self.record["terminal_feasibility"] = receipt
                        if not receipt["passed"]:
                            event("terminal_return_domain_violation",
                                  "entry already committed; continue existing finite guidance as unqualified best effort",
                                  terminal_feasibility=receipt)
                    if self.previous_budget is not None:
                        self.previous_budget["state"]["retained_return"] = "previous"
                        samples.append(self.previous_budget["state"])
                    observed["retained_return"] = "trigger"
                    samples.append(observed)
                    self.record.update(status="triggered", trigger={"time_s": s.time_s,
                        "state": observed, "budget": budget, "previous": self.previous_budget})
                    event("retained_return_terminal_trigger", "mass/state preparation estimate crossed; actual 6DOF outcome remains unverified",
                          policy_id=self.policy, required_altitude_m=budget["required_altitude_m"])
                else:
                    self.previous_budget = {"state": observed, "budget": budget}
            if flip_due:
                self.phase = "landing_burn"
                event("flip_and_landing_command", "generic PD + bounded engine/RCS actuation")
                return GuidanceStep(s, target, throttle, count, True, reference_diagnostics)
        elif self.phase == "landing_burn":
            vertical = env.dot(env.air_relative_velocity(ps), up)
            clearance = hull_clearance(s, v, p["geometry"]["ship_length_m"], p["geometry"]["radius_m"])["signed_clearance_m"]
            desired = -max(g["landing_target_speed_mps"], min(100., max(0., clearance)/g["landing_height_response_s"]))
            acceleration = env.norm(env.gravity_acceleration(s.r_eci_m))+(desired-vertical)/g["landing_velocity_response_s"]
            horizontal = env.add(env.air_relative_velocity(ps), env.scale(up, -vertical))
            lateral = env.scale(horizontal, -1/g["landing_horizontal_response_s"])
            lateral_limit = max(0., acceleration)*math.tan(math.radians(g["landing_max_tilt_deg"]))
            if env.norm(lateral) > lateral_limit:
                lateral = env.scale(lateral, lateral_limit/env.norm(lateral))
            landing_axis = env.add(env.scale(up, max(.1, acceleration)), lateral)
            target = _attitude(landing_axis, north)
            if self.return_frame is not None:
                # Preserve the transported roll reference during the terminal
                # axis change; do not inject a new geographic roll alignment.
                if self.policy in (CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID):
                    target = self.return_frame.target(landing_axis, north, target, time_s=s.time_s,
                        force_bridge=not self.landing_frame_started or self.policy == TRIMMED_POLICY_ID,
                        defer_geographic_reacquisition=self.policy == TRIMMED_POLICY_ID)
                    self.landing_frame_started = True
                else:
                    target = self.return_frame.target(landing_axis)
                reference_diagnostics = self.return_frame.diagnostics
            tilt = env.dot(dyn.rotate(s.q_body_to_eci, (0., 0., 1.)), up)
            if tilt > .5:
                force = max(0., acceleration)*o["mass_kg"]/tilt
                count = max(1, min(3, math.ceil(force/p["ship"]["engine_thrust_n"])))
                throttle = max(.4, min(1., force/(count*p["ship"]["engine_thrust_n"])))
            else:
                # Engines provide finite TVC authority for the flip; waiting
                # for alignment with all main engines off can strand the turn.
                throttle, count = g["flip_min_throttle"], 3
        return GuidanceStep(s, target, throttle, count, False, reference_diagnostics)

    def command(self, step, vehicle, profile, altitude):
        from .starship_sixdof_mission import control
        bounded = bool(self.allocation_enabled and self.record["status"] in ("active", "triggered")
                       and self.phase in ("ballistic_return", "landing_burn"))
        interval = control_interval(profile, step.throttle, altitude)
        command, diagnostics = control(step.state, vehicle, step.target, step.throttle, step.count, profile,
            use_flaps=self.phase in ("ballistic_return", "landing_burn"),
            development_fin_allocation=bounded, control_interval_s=interval,
            trim_angles_rad=self.entry_prepared["trim_angles_rad"] if self.entry_prepared is not None else None,
            development_entry_preposition=bounded and self.phase == "ballistic_return" and self.entry_prepared is not None,
            development_fin_policy="finite_moment_priority_fins_v1" if bounded else "finite_regularized_fins_v1")
        if step.reference_diagnostics is not None:
            diagnostics["attitude_reference"] = step.reference_diagnostics
        return command, diagnostics

    def to_dict(self):
        """Controller history only; restoration never grants flight authority."""
        value = deepcopy({k: v for k, v in vars(self).items() if k != "return_frame"})
        frame = self.return_frame
        if frame is None:
            saved = None
        elif isinstance(frame, ConditionedGeographicFrame):
            saved = {"type": "conditioned", "quaternion": frame.frame.quaternion,
                "frame_diagnostics": frame.frame.diagnostics, "time_s": frame.time_s,
                "maximum_roll_rate_rad_s": frame.maximum_roll_rate_rad_s,
                "bridging": frame.bridging, "diagnostics": frame.diagnostics}
        else:
            saved = {"type": "parallel", "quaternion": frame.quaternion, "diagnostics": frame.diagnostics}
        return json.loads(json.dumps({"schema": "missionos.ship_return_controller.v1", **value,
                                     "return_frame": saved}, allow_nan=False))

    @classmethod
    def from_dict(cls, value):
        expected = {f.name for f in fields(cls)} | {"schema"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("incomplete_return_controller_context")
        saved = json.loads(json.dumps(value, allow_nan=False))
        if saved.pop("schema", None) != "missionos.ship_return_controller.v1":
            raise ValueError("unknown_return_controller_schema")
        frame = saved.pop("return_frame")
        controller = cls(**saved)
        if frame is not None:
            q = tuple(frame["quaternion"])
            if len(q) != 4 or not all(math.isfinite(x) for x in q) or abs(sum(x*x for x in q)-1) > 1e-8:
                raise ValueError("invalid_saved_reference_frame")
            if frame["type"] == "conditioned":
                if type(frame["bridging"]) is not bool:
                    raise ValueError("invalid_saved_bridge_state")
                restored = ConditionedGeographicFrame(q, frame["time_s"], maximum_roll_rate_rad_s=frame["maximum_roll_rate_rad_s"])
                restored.frame.quaternion = q
                restored.frame.diagnostics = frame["frame_diagnostics"]
                restored.bridging = frame["bridging"]
            elif frame["type"] == "parallel":
                restored = ParallelTransportFrame(q)
                restored.quaternion = q
            else:
                raise ValueError("unknown_saved_reference_frame")
            restored.diagnostics = frame["diagnostics"]
            controller.return_frame = restored
        return controller


def control_interval(profile, throttle, altitude):
    return profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100000 else profile["integration"]["coast_dt_s"]


def record_sample(s, v, phase, command, diagnostics, samples, next_sample, profile):
    from .starship_sixdof_mission import _sample
    angular_due = bool(samples and env.norm(s.omega_body_rad_s)*(s.time_s-samples[-1]["time_s"]) > .2)
    if s.time_s >= next_sample-1e-9 or angular_due or not samples or samples[-1]["phase"] != phase:
        samples.append(_sample(s, v, phase, command, diagnostics))
        return s.time_s+profile["integration"]["sample_interval_s"]
    return next_sample


def advance_plant(s, v, command, dt, length, radius, altitude, air_speed):
    if altitude < max(2000., length+2*air_speed*dt):
        return find_contact(s, v, command, dt, length, radius)
    return dyn.step(s, v, command, dt), None

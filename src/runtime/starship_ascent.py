"""Shared ascent requests and an explicitly unqualified finite-burn screen.

This module requests bounded actuator commands. It never advances, replaces or
projects a plant state, dispatches a mission, or admits payload/return operation.
The opt-in schedule is a planned timing surrogate, not observed SpaceX guidance.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import math

import numpy as np

from . import starship_physics as env


@dataclass(frozen=True)
class AscentShutdown:
    """An absolute-time plant outage with an explicitly surrogate engine ID.

    Timing/source acceptance belongs to the preregistration, not this type. An
    accepted broadcast shutdown count does not establish any engine identifier.
    """

    engine_name: str
    time_s: float

    def __post_init__(self):
        allowed = {f"booster_main_{i}" for i in range(33)} | {f"ship_main_{i}" for i in (3, 4, 5)}
        if (type(self.engine_name) is not str or self.engine_name not in allowed
                or type(self.time_s) not in (int, float) or not math.isfinite(self.time_s)
                or not 0 < self.time_s < 800.):
            raise ValueError("invalid_ascent_shutdown")


@dataclass(frozen=True)
class AscentObjectives:
    """Distinct engineering endpoints for a suborbital burn and insertion.

    These values are caller-registered objectives, not inferred SpaceX data.
    The first burn must remain suborbital; the final bound-orbit gate is still
    bound to the profile's existing mission requirement by ``validate_profile``.
    """

    first_burn_altitude_m: float
    first_burn_perigee_m: float
    insertion_altitude_m: float
    final_perigee_m: float

    def __post_init__(self):
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (self.first_burn_altitude_m, self.first_burn_perigee_m,
                              self.insertion_altitude_m, self.final_perigee_m))
                or self.first_burn_altitude_m <= 0
                or not -env.EARTH_EQUATORIAL_RADIUS_M < self.first_burn_perigee_m < 0
                or not 0 < self.final_perigee_m <= self.insertion_altitude_m):
            raise ValueError("invalid_ascent_phase_objectives")


@dataclass(frozen=True)
class AscentCoastOrbitObjectives:
    """Engineering target ellipse for burn1; no fixed burn1 altitude plateau.

    Orbital radii use the same WGS84 equatorial reference as orbital_elements.
    Positive apogee headroom is a preflight necessary condition only; it does
    not predict the radius at a future restart or certify coast survival.
    """

    first_burn_perigee_m: float
    first_burn_apogee_m: float
    insertion_altitude_m: float
    final_perigee_m: float
    minimum_apogee_headroom_m: float

    def __post_init__(self):
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in asdict(self).values())
                or not -env.EARTH_EQUATORIAL_RADIUS_M < self.first_burn_perigee_m < 0
                or not 0 < self.final_perigee_m <= self.insertion_altitude_m
                or self.minimum_apogee_headroom_m <= 0
                or self.first_burn_apogee_m < self.final_perigee_m+self.minimum_apogee_headroom_m):
            raise ValueError("invalid_ascent_coast_orbit_objectives")


def coast_orbit_radial_reference(objectives, radius_m):
    """Two-body outbound radial reference, computed without advancing a state.

    v_r²=mu(2/r-1/a)-h²/r², h²=2*mu*rp*ra/(rp+ra). At that target ellipse,
    dv_r/dt=h²/r³-mu/r². Outside its radial interval the speed reference is
    explicitly clamped to zero, not a projection of the actual plant state.
    """
    if (type(objectives) is not AscentCoastOrbitObjectives or type(radius_m) not in (int, float)
            or not math.isfinite(radius_m) or radius_m <= 0):
        raise ValueError("invalid_ascent_coast_reference")
    rp = env.EARTH_EQUATORIAL_RADIUS_M+objectives.first_burn_perigee_m
    ra = env.EARTH_EQUATORIAL_RADIUS_M+objectives.first_burn_apogee_m
    a = (rp+ra)/2
    h2 = 2*env.EARTH_MU_M3_S2*rp*ra/(rp+ra)
    speed2 = env.EARTH_MU_M3_S2*(2/radius_m-1/a)-h2/radius_m**2
    return {"schema": "missionos.starship_ascent_coast_reference.v1",
        "frame": "geocentric_radial_inertial", "target_semimajor_axis_m": a,
        "target_angular_momentum_squared_m4_s2": h2,
        "radial_speed_squared_m2_s2": speed2,
        "desired_radial_velocity_mps": math.sqrt(max(0., speed2)),
        "radial_feedforward_acceleration_mps2": h2/radius_m**3-env.EARTH_MU_M3_S2/radius_m**2,
        "inside_target_radial_interval": rp <= radius_m <= ra,
        "actual_state_assigned": False, "future_coast_verified": False}


@dataclass(frozen=True)
class AscentControlOptions:
    """Explicit v6 control mechanisms, not new actuator authority.

    These switches support separately registered mechanism comparisons. They
    neither choose a fitted trajectory nor admit a whole-mission execution.
    """

    first_burn_only_bias: bool
    moving_reference_tracking: bool

    def __post_init__(self):
        if (type(self.first_burn_only_bias) is not bool
                or type(self.moving_reference_tracking) is not bool):
            raise ValueError("invalid_ascent_control_options")


@dataclass(frozen=True)
class StateInsertionOptions:
    """Registered engineering windows; not fitted physical parameters."""

    maximum_restart_time_s: float = 4000.
    maximum_burn_duration_s: float = 60.
    shutdown_settle_s: float = 5.
    maximum_radial_speed_mps: float = 20.
    maximum_attitude_error_deg: float = 5.
    maximum_body_rate_rad_s: float = .02

    def __post_init__(self):
        if (any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0
                for x in asdict(self).values())
                or self.maximum_attitude_error_deg >= 90.
                or self.maximum_restart_time_s+self.maximum_burn_duration_s+self.shutdown_settle_s > 4100.):
            raise ValueError("invalid_state_insertion_options")


def state_insertion_reference(s, craft, objectives, options):
    """Read the current orbit/attitude only; never predict or assign a state.

    The fixed 2 km radius window distinguishes apoapsis from periapsis. The
    target speed is the apoapsis speed of an ellipse whose perigee is the
    existing mission gate. It is a diagnostic, not a finite-burn proof.
    """
    from .starship_sixdof import rotate
    radius = env.norm(s.r_eci_m)
    up = env.unit(s.r_eci_m)
    radial = env.dot(s.v_eci_mps, up)
    tangent_vec = env.add(s.v_eci_mps, env.scale(up, -radial))
    if env.norm(tangent_vec) < 1.:
        raise ValueError("state_insertion_tangent_undefined")
    tangent = env.unit(tangent_vec)
    orbit = env.orbital_elements(env.State3D(s.time_s, s.r_eci_m, s.v_eci_mps, s.propellant_kg))
    axis = rotate(s.q_body_to_eci, (0., 0., 1.))
    angle = math.degrees(math.acos(max(-1., min(1., env.dot(axis, tangent)))))
    apogee = orbit.get("apogee_altitude_m")
    gap = None if apogee is None else env.EARTH_EQUATORIAL_RADIUS_M+apogee-radius
    rp = env.EARTH_EQUATORIAL_RADIUS_M+objectives.final_perigee_m
    speed = math.sqrt(2*env.EARTH_MU_M3_S2*rp/(radius*(radius+rp)))
    possible = (orbit.get("status") == "bound" and apogee is not None
        and apogee >= objectives.final_perigee_m+objectives.minimum_apogee_headroom_m)
    window = possible and -options.maximum_radial_speed_mps <= radial <= 0. and gap <= 2000.
    aligned = angle <= options.maximum_attitude_error_deg and env.norm(s.omega_body_rad_s) <= options.maximum_body_rate_rad_s
    return {"schema": "missionos.starship_state_insertion_reference.v1",
        "time_s": s.time_s, "frame": "geocentric_radial_inertial",
        "radius_m": radius, "radial_velocity_mps": radial,
        "tangential_velocity_mps": env.norm(tangent_vec), "tangent_eci": list(tangent),
        "target_ellipse_tangent_velocity_mps": speed,
        "tangential_velocity_error_mps": speed-env.norm(tangent_vec),
        "orbit_status": orbit.get("status"),
        "perigee_altitude_m": orbit.get("perigee_altitude_m"), "apogee_altitude_m": apogee,
        "apoapsis_radius_gap_m": gap, "orbit_has_insertion_headroom": bool(possible),
        "near_apoapsis_window": bool(window), "attitude_error_deg": angle,
        "body_rate_rad_s": env.norm(s.omega_body_rad_s), "aligned": aligned,
        "engine_available": bool(craft.engines and s.engine_states and s.engine_states[0].available),
        "actual_state_assigned": False, "finite_burn_feasibility_proven": False}


@dataclass(frozen=True)
class AscentScreenSettings:
    """Registered offsets, outages and an explicit opt-in phase objective.

    Legacy v2/v3 retain their exact record shape and guidance arithmetic.
    """

    stack_pitch_offset_deg: float = 0.
    ship_pitch_offset_deg: float = 0.
    finite_mode: bool = True
    policy: str = "fixed_schedule_v2"
    shutdowns: tuple[AscentShutdown, ...] = ()
    objectives: AscentObjectives | AscentCoastOrbitObjectives | None = None
    control_options: AscentControlOptions | None = None
    insertion_options: StateInsertionOptions | None = None

    def __post_init__(self):
        outcome_policy = self.policy in ("guidance_outcome_v3", "phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7")
        if (type(self.finite_mode) is not bool or self.policy not in ("fixed_schedule_v2", "guidance_outcome_v3", "phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7")
                or type(self.shutdowns) is not tuple
                or any(type(row) is not AscentShutdown for row in self.shutdowns)
                or len(self.shutdowns) > 2
                or len({row.engine_name.split("_", 1)[0] for row in self.shutdowns}) != len(self.shutdowns)
                or len({row.time_s for row in self.shutdowns}) != len(self.shutdowns)
                or tuple(row.time_s for row in self.shutdowns) != tuple(sorted(row.time_s for row in self.shutdowns))
                or not outcome_policy and self.shutdowns
                or not self.finite_mode and self.policy != "fixed_schedule_v2"
                or self.policy == "phase_objectives_v4" and type(self.objectives) is not AscentObjectives
                or self.policy in ("coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7") and type(self.objectives) is not AscentCoastOrbitObjectives
                or self.policy not in ("phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7") and self.objectives is not None
                or self.policy in ("controlled_coast_orbit_v6", "state_insertion_v7") and type(self.control_options) is not AscentControlOptions
                or self.policy not in ("controlled_coast_orbit_v6", "state_insertion_v7") and self.control_options is not None
                or self.policy == "state_insertion_v7" and (type(self.insertion_options) is not StateInsertionOptions
                    or not self.control_options.first_burn_only_bias)
                or self.policy != "state_insertion_v7" and self.insertion_options is not None
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (self.stack_pitch_offset_deg, self.ship_pitch_offset_deg))
                or self.finite_mode and (self.stack_pitch_offset_deg not in ((0, -5, -10, -15)
                                            if outcome_policy else (-5, -10, -15))
                                        or self.ship_pitch_offset_deg not in (0, -5, -10))
                or outcome_policy and self.stack_pitch_offset_deg == 0 and self.ship_pitch_offset_deg != 0
                or not self.finite_mode and (self.stack_pitch_offset_deg != 0
                                            or self.ship_pitch_offset_deg != 0)):
            raise ValueError("invalid_registered_ascent_screen_settings")

    def record(self):
        # Existing v2 records retain their three-field settings shape.
        result = {"stack_pitch_offset_deg": self.stack_pitch_offset_deg,
                  "ship_pitch_offset_deg": self.ship_pitch_offset_deg, "finite_mode": self.finite_mode}
        if self.policy in ("guidance_outcome_v3", "phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7"):
            result.update(policy=self.policy, shutdowns=[asdict(row) for row in self.shutdowns])
        if self.policy in ("phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7"):
            result["objectives"] = asdict(self.objectives)
        if self.policy in ("controlled_coast_orbit_v6", "state_insertion_v7"):
            result["control_options"] = asdict(self.control_options)
        if self.policy == "state_insertion_v7":
            result["insertion_options"] = asdict(self.insertion_options)
        return result


@dataclass(frozen=True)
class ExperimentalMissionAscentSettings:
    """First whole-mission seam for the frozen healthy R2 engineering law.

    This is separate from a physical-only screen, not permission to extend a
    screen or reuse a return certificate. Further objectives/outages need a new
    explicit version; they are not silently admitted by this initial seam.
    """

    ascent: AscentScreenSettings

    def __post_init__(self):
        expected = AscentScreenSettings(policy="coast_orbit_v5",
            objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.))
        if type(self.ascent) is not AscentScreenSettings or self.ascent != expected:
            raise ValueError("experimental_mission_requires_frozen_healthy_r2_ascent")

    def validate_profile(self, profile):
        AscentController(self.ascent).validate_profile(profile)
        if (profile["guidance"]["target_perigee_m"] != 220000.
                or profile["guidance"]["orbit_release_delay_s"] != 45.
                or profile["guidance"]["coast_before_return_s"] != 1800.
                or profile["payload"]["count"] != 26
                or profile["ship"]["return_reserve_kg"] != 28000.):
            raise ValueError("experimental_mission_profile_changes_frozen_gates_or_schedule")

    def record(self):
        return {"schema": "missionos.experimental_mission_ascent_settings.v1",
            "ascent": self.ascent.record(), "engineering_objectives_not_real_flight_identification": True,
            "qualified_return": False, "mission_authority_granted": False}


@dataclass(frozen=True)
class AscentRequest:
    target: tuple
    throttle: float
    count: int
    transition: str | None = None
    termination: str | None = None
    transition_reason: str | None = None
    guidance_diagnostic: dict | None = None


@dataclass
class AscentController:
    settings: AscentScreenSettings | None = None
    separation_time_s: float | None = None
    shutdown_receipts: list = field(default_factory=list)
    insertion_start_time_s: float | None = None

    def __post_init__(self):
        if self.settings is not None and type(self.settings) is not AscentScreenSettings:
            raise ValueError("invalid_ascent_screen_settings_type")

    @property
    def finite_mode(self):
        return self.settings is not None and self.settings.finite_mode

    @property
    def guidance_outcome_mode(self):
        return self.finite_mode and self.settings.policy in ("guidance_outcome_v3", "phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7")

    @property
    def phase_objectives_mode(self):
        return self.finite_mode and self.settings.policy in ("phase_objectives_v4", "coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7")

    @property
    def coast_orbit_mode(self):
        return self.finite_mode and self.settings.policy in ("coast_orbit_v5", "controlled_coast_orbit_v6", "state_insertion_v7")

    @property
    def control_v6_mode(self):
        return self.finite_mode and self.settings.policy == "controlled_coast_orbit_v6"

    @property
    def state_insertion_mode(self):
        return self.finite_mode and self.settings.policy == "state_insertion_v7"

    @property
    def control_trace_mode(self):
        return self.control_v6_mode or self.state_insertion_mode

    @property
    def moving_reference_tracking(self):
        return self.control_trace_mode and self.settings.control_options.moving_reference_tracking

    def validate_profile(self, profile):
        """Reject a caller-supplied final gate that changes mission acceptance."""
        if (self.phase_objectives_mode
                and self.settings.objectives.final_perigee_m != profile["guidance"]["target_perigee_m"]):
            raise ValueError("ascent_final_objective_changes_mission_gate")

    def before_step(self, s, v):
        """Apply only the frozen v3 availability outages, preserving spool/fuel.

        The state flag uses the existing plant failure mechanism. No throttle,
        gimbal, mass or engine specification is assigned a replacement value.
        """
        if not self.guidance_outcome_mode:
            return s
        applied = {row["engine_name"] for row in self.shutdown_receipts}
        for shutdown in self.settings.shutdowns:
            if shutdown.engine_name in applied or s.time_s < shutdown.time_s:
                continue
            names = [engine.name for engine in v.engines]
            if shutdown.engine_name not in names or len(s.engine_states) != len(names):
                raise ValueError("ascent_shutdown_target_not_in_current_body")
            if s.time_s != shutdown.time_s:
                raise ValueError("ascent_shutdown_missed_absolute_boundary")
            index = names.index(shutdown.engine_name)
            previous = s.engine_states[index]
            if not previous.available:
                raise ValueError("ascent_shutdown_target_already_unavailable")
            states = list(s.engine_states)
            states[index] = replace(previous, available=False)
            self.shutdown_receipts.append({"engine_name": shutdown.engine_name,
                "scheduled_at_s": shutdown.time_s, "applied_at_s": s.time_s,
                "engine_state_before": asdict(previous), "engine_state_after": asdict(states[index]),
                "engine_id_classification": "registered_representative_not_operator_engine_identifier"})
            s = replace(s, engine_states=tuple(states))
        return s

    def separated(self, time_s):
        if (self.separation_time_s is not None or type(time_s) not in (int, float)
                or not math.isfinite(time_s) or time_s < 0):
            raise ValueError("invalid_ascent_separation_time")
        self.separation_time_s = time_s

    def limit_step(self, time_s, interval_s):
        """Clip finite mode only; never round time or change an actuator state."""
        if not self.finite_mode:
            return interval_s
        boundaries = (1517., 1536.) if self.guidance_outcome_mode else (491., 1517., 1536.)
        if self.state_insertion_mode:
            options = self.settings.insertion_options
            boundaries = (options.maximum_restart_time_s,)
            if self.insertion_start_time_s is not None:
                boundaries += (self.insertion_start_time_s+options.maximum_burn_duration_s,)
        if self.guidance_outcome_mode:
            applied = {row["engine_name"] for row in self.shutdown_receipts}
            boundaries += tuple(row.time_s for row in self.settings.shutdowns if row.engine_name not in applied)
        for boundary in sorted(boundaries):
            if time_s < boundary < time_s+interval_s:
                return boundary-time_s
        return interval_s

    def request(self, s, v, p, phase, observation, up, east, north,
                radial, tangent_vec, tangent, orbit, attitude):
        """Consume current measured state and return a command/transition request.

        The default arithmetic is the former inline execution implementation.
        ``attitude`` is the flight executor's existing frame constructor; the
        controller neither owns an alternative allocator nor a plant integrator.
        """
        g = p["guidance"]
        self.validate_profile(p)
        target = attitude(tangent, north)
        if phase == "stack_ascent":
            program = g["pitch_program_time_deg"]
            values = [x[1] for x in program]
            if self.finite_mode:
                values = [angle if i < 2 else angle+self.settings.stack_pitch_offset_deg
                          for i, angle in enumerate(values)]
            pitch = float(np.interp(s.time_s, [x[0] for x in program], values))
            azimuth = math.radians(p["launch"]["azimuth_deg"])
            heading = env.add(env.scale(east, math.sin(azimuth)), env.scale(north, math.cos(azimuth)))
            direction = env.add(env.scale(up, math.sin(math.radians(pitch))),
                                env.scale(heading, math.cos(math.radians(pitch))))
            return AscentRequest(attitude(direction, north),
                                 .7 if observation["dynamic_pressure_pa"] > g["max_q_pa"] else 1., 33)
        if self.state_insertion_mode and phase in ("suborbital_coast", "orbit_insertion", "ascent_screen_shutdown"):
            return self._state_insertion_request(s, v, p, phase, north, attitude)
        if phase == "suborbital_coast":
            if s.propellant_kg <= p["ship"]["ascent_reserve_kg"]:
                return AscentRequest(target, 0., 0, termination="ascent_reserve_exhausted")
            if (self.phase_objectives_mode and orbit.get("status") == "bound"
                    and orbit["perigee_altitude_m"] >= self.settings.objectives.final_perigee_m):
                return AscentRequest(target, 0., 0, termination="insertion_gate_reached")
            return AscentRequest(target, 0., 0,
                                 transition="orbit_insertion" if s.time_s >= 1517. else None)
        if phase == "ascent_screen_shutdown":
            return AscentRequest(target, 0., 0)
        if phase not in ("ship_ascent", "orbit_insertion"):
            raise ValueError("unknown_ascent_controller_phase")
        if self.finite_mode:
            if s.propellant_kg <= p["ship"]["ascent_reserve_kg"]:
                return AscentRequest(target, 0., 0, termination="ascent_reserve_exhausted")
            if phase == "ship_ascent":
                if self.guidance_outcome_mode:
                    first_perigee = (self.settings.objectives.first_burn_perigee_m
                                     if self.phase_objectives_mode else g["target_perigee_m"])
                    apogee_met = (not self.coast_orbit_mode
                        or orbit.get("apogee_altitude_m") is not None
                        and orbit["apogee_altitude_m"] >= self.settings.objectives.first_burn_apogee_m)
                    if orbit.get("status") == "bound" and orbit["perigee_altitude_m"] >= first_perigee and apogee_met:
                        return AscentRequest(target, 0., 0, transition="suborbital_coast",
                            transition_reason=("measured_first_burn_coast_orbit_gates" if self.coast_orbit_mode else
                                               "measured_first_burn_suborbital_perigee_gate"
                                               if self.phase_objectives_mode else "measured_bound_orbit_perigee_gate"))
                    if s.time_s >= g["max_ascent_s"]:
                        return AscentRequest(target, 0., 0, termination="ascent_time_limit_exceeded")
                elif s.time_s >= 491.:
                    return AscentRequest(target, 0., 0, transition="suborbital_coast")
            if (phase == "orbit_insertion" and self.phase_objectives_mode
                    and orbit.get("status") == "bound"
                    and orbit["perigee_altitude_m"] >= self.settings.objectives.final_perigee_m):
                return AscentRequest(target, 0., 0, termination="insertion_gate_reached")
            if phase == "orbit_insertion" and s.time_s >= 1536.:
                reached = orbit.get("status") == "bound" and orbit["perigee_altitude_m"] >= g["target_perigee_m"]
                return AscentRequest(target, 0., 0,
                                     termination="insertion_gate_reached" if reached else "insertion_gate_unreached")
        guidance_diagnostic = None
        if self.coast_orbit_mode and phase == "ship_ascent":
            # Orbital radial velocity must use geocentric up, not ellipsoid
            # normal: the legacy geodetic target branch is left unchanged.
            up = env.unit(s.r_eci_m)
            radial = env.dot(s.v_eci_mps, up)
            tangent_vec = env.add(s.v_eci_mps, env.scale(up, -radial))
            tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
            guidance_diagnostic = coast_orbit_radial_reference(self.settings.objectives, env.norm(s.r_eci_m))
        tangential_speed = env.norm(tangent_vec)
        target_altitude = g["target_altitude_m"]
        if self.phase_objectives_mode:
            objectives = self.settings.objectives
            target_altitude = (objectives.first_burn_altitude_m if phase == "ship_ascent" and not self.coast_orbit_mode
                               else objectives.insertion_altitude_m)
        desired_vr = (guidance_diagnostic["desired_radial_velocity_mps"] if guidance_diagnostic is not None else
            max(-50., min(800., (target_altitude-observation["altitude_m"])/g["ascent_altitude_response_s"])))
        radial_acc = (env.EARTH_MU_M3_S2/env.norm(s.r_eci_m)**2
                      - tangential_speed**2/env.norm(s.r_eci_m)
                      + (desired_vr-radial)/g["ascent_velocity_response_s"])
        if guidance_diagnostic is not None:
            radial_acc += guidance_diagnostic["radial_feedforward_acceleration_mps2"]
            guidance_diagnostic = dict(guidance_diagnostic, actual_radial_velocity_mps=radial,
                requested_radial_thrust_acceleration_mps2=radial_acc)
        count = 1 if phase == "orbit_insertion" else 6
        if self.guidance_outcome_mode:
            if len(s.engine_states) != len(v.engines):
                raise ValueError("ascent_engine_state_count_mismatch")
            available_acc = sum(engine.max_thrust_n for index, (engine, actual) in enumerate(zip(v.engines, s.engine_states))
                if index < count and "main" in engine.name and actual.available)/observation["mass_kg"]
            if available_acc <= 0:
                return AscentRequest(target, 0., 0, termination="ascent_propulsion_unavailable")
        elif phase == "orbit_insertion":
            # control() selects the first existing main; use that same engine,
            # never the sum of engines that are not requested for this burn.
            available_acc = next(e.max_thrust_n for e in v.engines if "main" in e.name)/observation["mass_kg"]
        else:
            available_acc = sum(e.max_thrust_n for e in v.engines if "main" in e.name)/observation["mass_kg"]
        fraction = max(-.25, min(.95, radial_acc/available_acc))
        if (self.finite_mode and self.settings.ship_pitch_offset_deg
                and (not self.control_trace_mode or not self.settings.control_options.first_burn_only_bias
                     or phase == "ship_ascent")):
            if self.separation_time_s is None:
                raise ValueError("ascent_offset_requires_actual_separation")
            ramp = max(0., min(1., (s.time_s-self.separation_time_s)/10.))
            angle = math.atan2(fraction, math.sqrt(1-fraction*fraction))
            angle += math.radians(self.settings.ship_pitch_offset_deg)*ramp
            angle = max(math.asin(-.25), min(math.asin(.95), angle))
            fraction = math.sin(angle)
        direction = env.add(env.scale(up, fraction), env.scale(tangent, math.sqrt(1-fraction*fraction)))
        target = attitude(direction, north)
        if not self.finite_mode:
            if orbit.get("status") == "bound" and orbit["perigee_altitude_m"] >= g["target_perigee_m"]:
                return AscentRequest(target, 1., count, transition="orbital_coast")
            if s.propellant_kg <= p["ship"]["ascent_reserve_kg"] or s.time_s >= g["max_ascent_s"]:
                return AscentRequest(target, 1., count, transition="ballistic_return")
        return AscentRequest(target, 1., count, guidance_diagnostic=guidance_diagnostic)

    def _state_insertion_request(self, s, v, p, phase, north, attitude):
        options, objective = self.settings.insertion_options, self.settings.objectives
        diagnostic = state_insertion_reference(s, v, objective, options)
        target = attitude(tuple(diagnostic["tangent_eci"]), north)
        def result(throttle=0., count=0, **kwargs):
            return AscentRequest(target, throttle, count, guidance_diagnostic=diagnostic, **kwargs)
        if phase == "ascent_screen_shutdown":
            return result()
        if s.propellant_kg <= p["ship"]["ascent_reserve_kg"]:
            return result(termination="ascent_reserve_exhausted")
        if diagnostic["orbit_status"] == "bound" and diagnostic["perigee_altitude_m"] >= objective.final_perigee_m:
            return result(termination="insertion_gate_reached")
        if diagnostic["orbit_status"] != "bound":
            return result(termination="state_insertion_unavailable")
        if phase == "suborbital_coast":
            if s.time_s >= options.maximum_restart_time_s:
                return result(termination="state_insertion_deadline")
            if not diagnostic["orbit_has_insertion_headroom"] or not diagnostic["engine_available"]:
                return result(termination="state_insertion_unavailable")
            if diagnostic["radial_velocity_mps"] <= 0 and (
                    diagnostic["radial_velocity_mps"] < -options.maximum_radial_speed_mps
                    or diagnostic["apoapsis_radius_gap_m"] > 2000.):
                return result(termination="state_insertion_window_missed")
            if diagnostic["near_apoapsis_window"] and diagnostic["aligned"]:
                if diagnostic["tangential_velocity_error_mps"] <= 0:
                    return result(termination="state_insertion_unavailable")
                self.insertion_start_time_s = s.time_s
                return result(transition="orbit_insertion", transition_reason="measured_apoapsis_insertion_window")
            return result()
        if self.insertion_start_time_s is None:
            raise ValueError("state_insertion_without_measured_restart")
        if s.time_s >= self.insertion_start_time_s+options.maximum_burn_duration_s:
            return result(termination="state_insertion_burn_limit")
        if not diagnostic["engine_available"]:
            return result(termination="state_insertion_unavailable")
        throttle = v.engines[0].min_throttle
        if not 0 < throttle <= 1:
            raise ValueError("state_insertion_main_minimum_throttle_invalid")
        return result(throttle, 1)

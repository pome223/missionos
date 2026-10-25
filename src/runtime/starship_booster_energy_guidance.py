"""Opt-in, fact-only Booster guidance; requests are never achieved state.

The point screen and a future finite-actuator adapter share these equations.
No integrator, vehicle mutation, model provider or operational authority lives
here. The physical authority adapter supplies the attitude bounds separately.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from numbers import Real

POLICY_ID = "availability_energy_water_return_v2"
NORTH_ENU = (0., 1., 0.)
UP_ENU = (0., 0., 1.)
PHASES = ("booster_boostback_slew", "booster_boostback", "booster_entry_coast",
          "booster_landing_align", "booster_landing_burn")
SETTLE_PHASE = "booster_boostback_settle"
PHASE_ORDER = (*PHASES[:2], SETTLE_PHASE, *PHASES[2:])
HANDOFF_MAXIMUM_DURATION_S = 30.  # Existing recovery rate_settle_max_s.
HANDOFF_MAXIMUM_BODY_RATE_RAD_S = .003  # Existing recovery rate_settle_limit_rad_s.


def _number(value, name, *, positive=False):
    if (not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value)
            or positive and value <= 0):
        raise ValueError(f"invalid_{name}")
    return float(value)


def _v(value):
    result = tuple(_number(x, "vector") for x in value)
    if len(result) != 3:
        raise ValueError("three_vector_required")
    return result


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(v):
    return math.hypot(*v)


def _scale(v, k):
    return tuple(x*k for x in v)


def _add(a, b):
    return tuple(x+y for x, y in zip(a, b))


def _unit(v):
    length = _norm(v)
    if length < 1e-12:
        raise ValueError("nonzero_direction_required")
    return _scale(v, 1/length)


def _cap(v, maximum):
    return _scale(v, min(1., maximum/max(_norm(v), 1e-30)))


def body_axes(axis, reference=NORTH_ENU):
    """Columns of the actual ``_attitude(axis, north)`` target convention."""
    z, reference = _unit(_v(axis)), _v(reference)
    x = _add(reference, _scale(z, -_dot(reference, z)))
    if _norm(x) < 1e-6:
        fallback = tuple(float(i == min(range(3), key=lambda j: abs(z[j]))) for i in range(3))
        x = _add(fallback, _scale(z, -_dot(fallback, z)))
    x = _unit(x)
    y = (z[1]*x[2]-z[2]*x[1], z[2]*x[0]-z[0]*x[2], z[0]*x[1]-z[1]*x[0])
    return x, y, z


@dataclass(frozen=True)
class BoosterEnergyControlSettings:
    boostback_vertical_target_mps: float
    boostback_velocity_tau_s: float
    handoff_main_count: int = 0

    def __post_init__(self):
        vz = _number(self.boostback_vertical_target_mps, "vertical_target")
        tau = _number(self.boostback_velocity_tau_s, "velocity_tau", positive=True)
        if (vz not in (500., 750.) or tau != 10. or type(self.handoff_main_count) is not int
                or self.handoff_main_count not in (0, 1, 3)):
            raise ValueError("unregistered_energy_control_family")

    def record(self):
        return {"schema": "missionos.booster_energy_control_settings.v1", "policy_id": POLICY_ID,
                "boostback_vertical_target_mps": float(self.boostback_vertical_target_mps),
                "boostback_velocity_tau_s": float(self.boostback_velocity_tau_s),
                **({"handoff_main_count": self.handoff_main_count} if self.handoff_main_count else {})}


@dataclass(frozen=True)
class MainCapability:
    max_thrust_n: float
    min_throttle: float
    available: bool

    def __post_init__(self):
        _number(self.max_thrust_n, "max_thrust", positive=True)
        if type(self.available) is not bool or not 0 <= _number(self.min_throttle, "min_throttle") <= 1:
            raise ValueError("invalid_main_capability")


@dataclass(frozen=True)
class GuidanceObservation:
    time_s: float
    position_enu_m: tuple[float, float, float]
    velocity_enu_mps: tuple[float, float, float]
    clearance_m: float
    mass_kg: float
    propellant_kg: float
    gravity_mps2: float
    body_axis_enu: tuple[float, float, float]
    body_rate_rad_s: float
    dynamic_pressure_pa: float
    actual_alpha_from_tail_first_deg: float
    inertia_bound_kg_m2: float
    engine_lever_m: float
    mains: tuple[MainCapability, ...]

    def __post_init__(self):
        for key in ("time_s", "clearance_m", "propellant_kg", "body_rate_rad_s",
                    "dynamic_pressure_pa", "actual_alpha_from_tail_first_deg", "engine_lever_m"):
            _number(getattr(self, key), key)
        for key in ("mass_kg", "gravity_mps2", "inertia_bound_kg_m2"):
            _number(getattr(self, key), key, positive=True)
        for key in ("position_enu_m", "velocity_enu_mps", "body_axis_enu"):
            if type(getattr(self, key)) is not tuple:
                raise ValueError("immutable_guidance_vectors_required")
            _v(getattr(self, key))
        if (abs(_norm(self.body_axis_enu)-1.) > 1e-6 or self.propellant_kg < 0
                or min(self.body_rate_rad_s, self.dynamic_pressure_pa, self.engine_lever_m) < 0
                or not 0 <= self.actual_alpha_from_tail_first_deg <= 180
                or self.mass_kg < self.propellant_kg or type(self.mains) is not tuple
                or len(self.mains) != 33 or any(type(x) is not MainCapability for x in self.mains)):
            raise ValueError("invalid_guidance_observation")


@dataclass(frozen=True)
class LandingPreview:
    complete_arrest: bool
    stopping_distance_m: float
    minimum_altitude_m: float
    remaining_propellant_kg: float
    termination: str

    def admissible(self, minimum_propellant_kg):
        if type(self.complete_arrest) is not bool:
            raise ValueError("preview_flag_must_be_boolean")
        for name in ("stopping_distance_m", "minimum_altitude_m", "remaining_propellant_kg"):
            _number(getattr(self, name), name)
        return (self.complete_arrest and self.termination == "arrested"
                and self.stopping_distance_m >= 0 and self.minimum_altitude_m >= 0
                and self.remaining_propellant_kg >= minimum_propellant_kg)


@dataclass(frozen=True)
class GuidanceMemory:
    origin_time_s: float
    phase: str = PHASES[0]
    boostback_started_s: float | None = None
    aerobraking_started: bool = False
    last_time_s: float | None = None
    handoff_started_s: float | None = None
    handoff_status: str = "not_requested"

    def __post_init__(self):
        _number(self.origin_time_s, "origin_time")
        if (self.phase not in PHASE_ORDER or type(self.aerobraking_started) is not bool
                or self.handoff_status not in ("not_requested", "pending", "completed", "incomplete_timeout", "incomplete_reserve")
                or self.handoff_status != "not_requested" and self.handoff_started_s is None):
            raise ValueError("invalid_guidance_memory")
        for value in (self.boostback_started_s, self.last_time_s, self.handoff_started_s):
            if value is not None and _number(value, "memory_time") < self.origin_time_s:
                raise ValueError("guidance_memory_precedes_origin")

    @property
    def handoff_completed(self):
        return self.handoff_status == "completed"

    @property
    def handoff_incomplete(self):
        return self.handoff_status in ("incomplete_timeout", "incomplete_reserve")

    @property
    def handoff_guard_reason(self):
        return {"incomplete_timeout": "handoff_time_guard", "incomplete_reserve": "handoff_reserve_guard"}.get(self.handoff_status)


@dataclass(frozen=True)
class GuidanceRequest:
    memory: GuidanceMemory
    target_axis_enu: tuple[float, float, float]
    main_prefix_count: int
    main_throttle: float
    diagnostics: dict


def available_prefix(mains, force_n, maximum_count):
    """Count is a physical prefix index, never a compact available count."""
    if force_n <= 0:
        return 0, 0.
    supply = 0.
    for count in range(1, maximum_count+1):
        engine = mains[count-1]
        if engine.available:
            supply += engine.max_thrust_n
        if supply >= force_n:
            break
    if supply <= 0:
        return count, 0.
    floor = max((e.min_throttle for e in mains[:count] if e.available), default=0.)
    return count, max(floor, min(1., force_n/supply))


def boostback_target(observation, settings):
    """Recompute horizontal target from CURRENT displacement after finite slew."""
    h, g, vz = max(0., observation.position_enu_m[2]), observation.gravity_mps2, settings.boostback_vertical_target_mps
    fall = max(30., min(400., (vz+math.sqrt(vz*vz+2*g*h))/g))
    return (*_cap(_scale(observation.position_enu_m[:2], -1/fall), 700.), float(vz)), fall


def actuator_response_delay(actuators):
    return (3*actuators["throttle_tau_s"]+1/actuators["throttle_rate_s"]
            +3*actuators["gimbal_tau_s"]+actuators["max_gimbal_deg"]/actuators["gimbal_rate_deg_s"])


def horizontal_terminal(observation, area_radius_m, actuators):
    tau = 8.  # Existing landing_horizontal_tau_s, not a fit parameter.
    delay = actuator_response_delay(actuators)
    p, velocity = observation.position_enu_m[:2], observation.velocity_enu_mps[:2]
    footprint = _add(p, _scale(velocity, tau+delay))
    inside = _norm(p) <= area_radius_m and _norm(footprint) <= area_radius_m
    acceleration = (_scale(velocity, -1/tau) if inside else
                    _add(_scale(p, -1/(tau*tau)), _scale(velocity, -2/tau)))
    return acceleration, {"area_velocity_damping": bool(inside), "braking_footprint_enu_m": list(footprint),
                          "actuator_response_delay_s": delay}


def vertical_terminal(observation, actuators, powered_dt_s, variant):
    vz, g = observation.velocity_enu_mps[2], observation.gravity_mps2
    if variant == "legacy_vertical":
        desired = -max(1.5, min(100., max(0., observation.clearance_m)/6))
        return max(.5, g+(desired-vz)/1.5)
    if variant != "signed_energy":
        raise ValueError("unknown_energy_terminal_variant")
    down = max(0., -vz)
    effective = max(.1, observation.clearance_m-down*(actuators["throttle_tau_s"]+powered_dt_s))
    acceleration = g+max(0., (down*down-1.5**2)/(2*effective))
    if vz >= -1.5:
        acceleration += (-1.5-vz)/1.5
    return max(0., acceleration)  # Request only; actual finite shutdown tail is not removed.


def finite_alignment_lead(observation, actuators, guidance):
    available = sum(e.max_thrust_n*max(.4, e.min_throttle) for e in observation.mains[:3] if e.available)
    torque = 2*actuators["rcs_radius_m"]*actuators["rcs_thrust_n"]
    torque += available*observation.engine_lever_m*math.sin(math.radians(actuators["max_gimbal_deg"]))
    alpha = min(guidance["max_angular_acceleration_rad_s2"], torque/observation.inertia_bound_kg_m2)
    if alpha <= 0:
        return math.inf
    rate = observation.body_rate_rad_s
    braking_time = rate/alpha
    angle = math.acos(max(-1., min(1., observation.body_axis_enu[2])))+math.radians(15.)+rate*rate/(2*alpha)
    maximum_rate = math.sqrt(2*alpha*math.pi)
    switch = maximum_rate*maximum_rate/alpha
    slew = (2*math.sqrt(angle/alpha) if angle <= switch else
            2*maximum_rate/alpha+(angle-switch)/maximum_rate)
    return (braking_time+slew+4/(guidance["attitude_damping_ratio"]*guidance["attitude_frequency_rad_s"])
            +actuator_response_delay(actuators))


def attitude_pd(error_quaternion, actual_rate_body_rad_s, *, frequency_rad_s, damping_ratio, alpha_rad_s2):
    """Exactly the existing component-clipped PD law, shared by point/adapter."""
    if len(error_quaternion) != 4:
        raise ValueError("quaternion_required")
    error = tuple(_number(x, "quaternion") for x in error_quaternion)
    if error[0] < 0:
        error = tuple(-x for x in error)
    actual = _v(actual_rate_body_rad_s)
    for value in (frequency_rad_s, damping_ratio, alpha_rad_s2):
        _number(value, "attitude_bound", positive=True)
    raw = tuple(2*frequency_rad_s**2*e-2*damping_ratio*frequency_rad_s*w for e, w in zip(error[1:], actual))
    return tuple(max(-alpha_rad_s2, min(alpha_rad_s2, x)) for x in raw)


def entry_axis(observation, panels, *, isp_s, com_body_m, rcs_pair_torque_nm, minimum_propellant_kg,
               previous_axis=None, prepare_descent=False):
    """One entry helper; optional future descent is a target, never state."""
    from .starship_booster_recovery import _entry_axis
    if observation.propellant_kg < minimum_propellant_kg:
        return UP_ENU, {"entry_angle_deg": 0., "candidate_count": 0,
            "entry_admissible": False, "entry_termination": "insufficient_entry_reserve",
            "actual_propellant_below_required_reserve": True,
            "required_propellant_reserve_kg": float(minimum_propellant_kg)}
    if type(prepare_descent) is not bool:
        raise ValueError("entry_descent_preparation_must_be_boolean")
    planned_position, planned_velocity = observation.position_enu_m, observation.velocity_enu_mps
    preparation_time, preparation_mode = 0., "current_state"
    height, vertical, gravity = observation.position_enu_m[2], observation.velocity_enu_mps[2], observation.gravity_mps2
    reachable_80km = height+vertical*vertical/(2*gravity) > 80000.
    project_80km = prepare_descent and (height > 80000. or vertical >= 0 and reachable_80km)
    if project_80km:
        discriminant = max(0., vertical*vertical+2*gravity*(height-80000.))
        preparation_time = (vertical+math.sqrt(discriminant))/gravity
        planned_position = (observation.position_enu_m[0]+observation.velocity_enu_mps[0]*preparation_time,
                            observation.position_enu_m[1]+observation.velocity_enu_mps[1]*preparation_time, 80000.)
        planned_velocity = (*observation.velocity_enu_mps[:2], vertical-gravity*preparation_time)
        preparation_mode = "future_descending_80km"
    elif prepare_descent and vertical >= 0 and height <= 80000.:
        preparation_mode = "current_state_below_unreachable_80km"
    axis, diagnostic = _entry_axis(planned_position, planned_velocity, observation.mass_kg,
        observation.propellant_kg, panels, isp_s=isp_s, previous=previous_axis,
        trim_context={"com_body_m": com_body_m, "jet_torque_nm": rcs_pair_torque_nm,
                      "minimum_trim_dynamic_pressure_pa": 20000.},
        candidate_angles_deg=(10., 20., 30.), reserve_propellant_kg=minimum_propellant_kg,
        reference_axis_enu=NORTH_ENU)
    return axis, {**diagnostic, "entry_admissible": True, "actual_propellant_below_required_reserve": False,
                  "required_propellant_reserve_kg": float(minimum_propellant_kg),
                  "future_descent_preparation_enabled": prepare_descent,
                  "future_descent_preparation_time_s": float(preparation_time),
                  "future_descent_preparation_mode": preparation_mode,
                  "planned_descent_position_enu_m": list(planned_position),
                  "planned_descent_velocity_enu_mps": list(planned_velocity),
                  "planned_target_is_actual_state": False}


def advance_guidance(observation, memory, settings, *, actuators, guidance, entry_axis_enu,
                     entry_angle_deg, preview, minimum_propellant_kg, area_radius_m,
                     powered_dt_s, terminal_variant="signed_energy", entry_attitude_error_deg=None):
    """A pure transition/request. The caller applies it through finite actuators."""
    if (type(observation) is not GuidanceObservation or type(memory) is not GuidanceMemory
            or type(settings) is not BoosterEnergyControlSettings or type(preview) is not LandingPreview):
        raise ValueError("exact_energy_guidance_types_required")
    if observation.time_s < (memory.last_time_s if memory.last_time_s is not None else memory.origin_time_s):
        raise ValueError("guidance_clock_regressed")
    if settings.handoff_main_count:
        if entry_attitude_error_deg is None or not 0 <= _number(entry_attitude_error_deg, "entry_attitude_error") <= 180:
            raise ValueError("powered_handoff_requires_actual_full_attitude_error")
    elif memory.phase == SETTLE_PHASE:
        raise ValueError("legacy_energy_settings_cannot_enter_powered_handoff")
    previous_phase = memory.phase
    handoff_shutdown_this_tick = False
    memory = replace(memory, last_time_s=observation.time_s)
    target, throttle, count = UP_ENU, 0., 0
    diagnostic = {"previous_phase": previous_phase, "policy_id": POLICY_ID, "preview_is_execution": False, "state_assigned": False}
    if memory.phase in PHASES[:2]:
        endpoint, fall = boostback_target(observation, settings)
        error = _add(endpoint, _scale(observation.velocity_enu_mps, -1.))
        elapsed = None if memory.boostback_started_s is None else observation.time_s-memory.boostback_started_s
        reason = ("target_attained" if _norm(error) <= 30. else "reserve_guard" if observation.propellant_kg <= 60000.
                  else "burn_time_guard" if elapsed is not None and elapsed >= 70.
                  else "slew_timeout" if elapsed is None and observation.time_s-memory.origin_time_s >= 90. else None)
        diagnostic.update(boostback_target_velocity_enu_mps=list(endpoint), boostback_velocity_error_mps=_norm(error),
                          fall_time_s=fall, cutoff_reason=reason)
        if reason is not None:
            memory = replace(memory, phase=SETTLE_PHASE if settings.handoff_main_count else PHASES[2],
                handoff_started_s=(observation.time_s if settings.handoff_main_count else memory.handoff_started_s),
                handoff_status=("pending" if settings.handoff_main_count else memory.handoff_status))
        else:
            force = _add(_scale(error, 1/settings.boostback_velocity_tau_s), (0., 0., observation.gravity_mps2))
            target = _unit(force)
            if _dot(observation.body_axis_enu, target) >= math.cos(math.radians(20.)):
                memory = replace(memory, phase=PHASES[1], boostback_started_s=(observation.time_s
                    if memory.boostback_started_s is None else memory.boostback_started_s))
                count, throttle = available_prefix(observation.mains, observation.mass_kg*_norm(force), 33)
            else:
                count, throttle = 3, max(.4, *(e.min_throttle for e in observation.mains[:3]))
    if memory.phase == SETTLE_PHASE:
        if memory.handoff_started_s is None:
            raise ValueError("powered_handoff_requires_start_time")
        target = _unit(_v(entry_axis_enu))
        elapsed = observation.time_s-memory.handoff_started_s
        completed = entry_attitude_error_deg <= 5. and observation.body_rate_rad_s <= HANDOFF_MAXIMUM_BODY_RATE_RAD_S
        guard = ("handoff_reserve_guard" if observation.propellant_kg <= 60000. else
                 "handoff_time_guard" if elapsed >= HANDOFF_MAXIMUM_DURATION_S else None)
        diagnostic.update(handoff_elapsed_s=float(elapsed),
            handoff_full_attitude_error_deg=float(entry_attitude_error_deg),
            handoff_actual_body_rate_rad_s=float(observation.body_rate_rad_s),
            handoff_requested_main_prefix=settings.handoff_main_count,
            handoff_completed=bool(completed and guard is None), handoff_guard_reason=guard)
        if guard is not None or completed:
            status = ("incomplete_reserve" if guard == "handoff_reserve_guard" else
                      "incomplete_timeout" if guard == "handoff_time_guard" else "completed")
            memory = replace(memory, phase=PHASES[2], handoff_status=status)
            handoff_shutdown_this_tick = True
        else:
            count = settings.handoff_main_count
            throttle = max(.4, *(e.min_throttle for e in observation.mains[:count]))
    if memory.phase == PHASES[2]:
        target = _unit(_v(entry_axis_enu))  # Never overwritten by a generic upright-coast block.
        if (observation.velocity_enu_mps[2] < 0 and observation.dynamic_pressure_pa >= 500.
                and observation.actual_alpha_from_tail_first_deg >= 5. and entry_angle_deg >= 10.):
            memory = replace(memory, aerobraking_started=True)
        admitted = preview.admissible(minimum_propellant_kg)
        diagnostic["complete_above_ground_reserve_preview"] = bool(admitted)
        if (memory.aerobraking_started and admitted and observation.velocity_enu_mps[2] < 0
                and not handoff_shutdown_this_tick):
            lead = finite_alignment_lead(observation, actuators, guidance)
            down = -observation.velocity_enu_mps[2]
            diagnostic["finite_alignment_lead_s"] = lead
            if observation.clearance_m <= preview.stopping_distance_m+down*lead+.5*observation.gravity_mps2*lead**2+300.:
                memory = replace(memory, phase=PHASES[3])
    if memory.phase == PHASES[3]:
        target = UP_ENU
        error = math.acos(max(-1., min(1., observation.body_axis_enu[2])))
        if error > math.radians(5.) or observation.body_rate_rad_s > .02:
            count, throttle = 3, max(.4, *(e.min_throttle for e in observation.mains[:3]))
        if (error <= math.radians(5.) and observation.body_rate_rad_s <= .02
                and preview.admissible(minimum_propellant_kg)
                and observation.clearance_m <= preview.stopping_distance_m+300.):
            memory = replace(memory, phase=PHASES[4])
    if memory.phase == PHASES[4]:
        upward = vertical_terminal(observation, actuators, powered_dt_s, terminal_variant)
        horizontal, area_diagnostic = horizontal_terminal(observation, area_radius_m, actuators)
        diagnostic.update(area_diagnostic, upward_thrust_acceleration_mps2=upward)
        horizontal = _cap(horizontal, upward*math.tan(math.radians(15.)))
        force = (*horizontal, upward)
        if _norm(force) <= 1e-12:
            target, count, throttle = UP_ENU, 0, 0.
        else:
            target = _unit(force)
            alignment = _dot(observation.body_axis_enu, target)
            if alignment > .5:
                count, throttle = available_prefix(observation.mains, observation.mass_kg*_norm(force)/alignment, 13)
            else:
                count, throttle = 3, max(.4, *(e.min_throttle for e in observation.mains[:3]))
    if PHASE_ORDER.index(memory.phase) < PHASE_ORDER.index(diagnostic.get("previous_phase", memory.phase)):
        raise ValueError("guidance_phase_regressed")
    return GuidanceRequest(memory, tuple(float(x) for x in target), int(count), float(throttle), diagnostic)

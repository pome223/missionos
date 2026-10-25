"""Opt-in measured allocation and separately declared low-pressure capture.

Healthy vehicles use exactly the legacy controller. An actual unavailable
main selects measured finite-thrust TVC; requests never assign achieved state.
This is an experimental control repair, not refreshed return qualification.
"""
from dataclasses import asdict, dataclass
import math

import numpy as np

from . import starship_physics as env, starship_sixdof as dyn
from .starship_return_sites import ReturnSites, validate_return_site
from .starship_splashdown import SplashdownGoal

POLICY_ID = "availability_terminal_recovery_v1"
MODES = {"low_pressure_residual_capture", "powered_reference_capture", "powered_reference_shutdown_tail", "low_pressure_reference_capture", "legacy_healthy_control", "legacy_degraded_control", "measured_tvc_shutdown_tail",
         "measured_tvc_recentered_coast", "measured_tvc_available_main_control"}
PHASES = {"booster_boostback_slew", "booster_boostback", "booster_entry_coast", "booster_landing_burn"}


@dataclass(frozen=True)
class BoosterTerminalRecoverySettings:
    """Explicit control repairs; no new physical values or fitted gains."""

    powered_terminal_alignment: bool = True
    shutdown_tail_only: bool = False
    low_pressure_reference_capture: bool = False
    powered_reference_capture: bool = False
    residual_authority_capture: bool = False
    energy_terminal_descent: bool = False
    planned_capture_impulse_compensation: bool = False
    terminal_horizontal_braking: bool = False

    def __post_init__(self):
        if type(self.powered_terminal_alignment) is not bool:
            raise ValueError("powered_terminal_alignment_must_be_boolean")
        if type(self.shutdown_tail_only) is not bool:
            raise ValueError("shutdown_tail_only_must_be_boolean")
        if type(self.low_pressure_reference_capture) is not bool:
            raise ValueError("low_pressure_reference_capture_must_be_boolean")
        if type(self.powered_reference_capture) is not bool:
            raise ValueError("powered_reference_capture_must_be_boolean")
        if type(self.residual_authority_capture) is not bool:
            raise ValueError("residual_authority_capture_must_be_boolean")
        if type(self.energy_terminal_descent) is not bool:
            raise ValueError("energy_terminal_descent_must_be_boolean")
        if type(self.planned_capture_impulse_compensation) is not bool:
            raise ValueError("planned_capture_impulse_compensation_must_be_boolean")
        if type(self.terminal_horizontal_braking) is not bool:
            raise ValueError("terminal_horizontal_braking_must_be_boolean")
        if self.terminal_horizontal_braking and not self.planned_capture_impulse_compensation:
            raise ValueError("terminal_horizontal_braking_requires_planned_capture_impulse_compensation")
        if self.planned_capture_impulse_compensation and not (self.powered_reference_capture and self.energy_terminal_descent):
            raise ValueError("planned_capture_impulse_compensation_requires_powered_capture_and_energy_descent")
        if self.energy_terminal_descent and not self.residual_authority_capture:
            raise ValueError("energy_terminal_descent_requires_residual_authority_capture")
        if self.residual_authority_capture and not self.low_pressure_reference_capture:
            raise ValueError("residual_authority_capture_requires_low_pressure_reference_capture")
        if self.powered_reference_capture and not self.low_pressure_reference_capture:
            raise ValueError("powered_reference_capture_requires_low_pressure_reference_capture")
        if self.low_pressure_reference_capture and (self.powered_terminal_alignment or not self.shutdown_tail_only):
            raise ValueError("reference_capture_requires_original_alignment_and_tail_only_allocation")

    def record(self):
        return {"schema": "missionos.booster_terminal_recovery_settings.v1", "policy_id": POLICY_ID,
                **({"powered_terminal_alignment": False} if not self.powered_terminal_alignment else {}),
                **({"shutdown_tail_only": True} if self.shutdown_tail_only else {}),
                **({"low_pressure_reference_capture": True} if self.low_pressure_reference_capture else {}),
                **({"powered_reference_capture": True} if self.powered_reference_capture else {}),
                **({"residual_authority_capture": True} if self.residual_authority_capture else {}),
                **({"energy_terminal_descent": True} if self.energy_terminal_descent else {}),
                **({"planned_capture_impulse_compensation": True} if self.planned_capture_impulse_compensation else {}),
                **({"terminal_horizontal_braking": True} if self.terminal_horizontal_braking else {})}


def validate_terminal_scope(profile, settings, return_sites, splashdown_goal):
    if (type(settings) is not BoosterTerminalRecoverySettings or type(return_sites) is not ReturnSites
            or type(splashdown_goal) is not SplashdownGoal or profile["booster"]["engine_count"] != 33):
        raise ValueError("terminal_recovery_requires_exact_settings_33_mains_and_bound_water_goal")
    validate_return_site(return_sites.divert, profile, terminal_goal="model_surface_contact")
    splashdown_goal.validate_site(return_sites.divert)


def terminal_control_command(state, craft, target, throttle, count, profile, *, settings,
                             phase, observed, control_interval_s, coast_started_s=None, reference_capture_active=False,
                             reference_capture_closed=False, capture_reference=None, powered_capture_request=None,
                             powered_tail_tracking=False):
    """Read actual availability/loads and submit finite commands, without a step.

    The default changes allocation only for a vehicle with an unavailable main.
    The separately declared capture option changes low-pressure requests only;
    the caller retains all physical bounds and the finite integration steps.
    Current shutdown moments always enter the measured residual; strong finite
    gimbal-tail authority is retained until the unchanged physical RCS pairs
    can cover its capacity, avoiding authority from asymptotically tiny spool.
    """
    if (type(settings) is not BoosterTerminalRecoverySettings or phase not in PHASES
            or type(control_interval_s) not in (int, float) or isinstance(control_interval_s, bool)
            or not math.isfinite(control_interval_s) or not 0 < control_interval_s <= 10):
        raise ValueError("invalid_terminal_control_scope")
    if (len(craft.engines) < 33 or len(state.engine_states) != len(craft.engines)
            or [e.name for e in craft.engines[:33]] != [f"booster_main_{i}" for i in range(33)]):
        raise ValueError("terminal_recovery_requires_original_index_main_inventory")
    fresh = dyn.observe(state, craft)
    for key in ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
                "propellant_kg", "com_body_m", "inertia_kg_m2", "thrust_force_body_n", "thrust_torque_body_nm"):
        if not np.array_equal(observed[key], fresh[key]):
            raise ValueError("terminal_control_observation_is_not_actual_state")
    observed = fresh
    unavailable = [i for i, row in enumerate(state.engine_states[:33]) if not row.available]
    tvc_capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
        *math.sin(engine.max_gimbal_rad) for engine, actual, load in
        zip(craft.engines, state.engine_states, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0))
    main_thrust = float(sum(load["thrust_n"] for engine, load in zip(craft.engines, observed["engine_loads"])
                            if not engine.name.startswith("rcs_")))
    jet_capacity = float(2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"])
    uncommanded_tvc_capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
        *math.sin(engine.max_gimbal_rad) for i, (engine, actual, load) in
        enumerate(zip(craft.engines, state.engine_states, observed["engine_loads"]))
        if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0
        and not (i < count and throttle > 0)))
    active = bool(unavailable and (not settings.shutdown_tail_only or uncommanded_tvc_capacity > jet_capacity))
    tail = bool(active and phase == "booster_entry_coast" and tvc_capacity > jet_capacity)
    use_flaps = phase in ("booster_entry_coast", "booster_landing_burn")
    if type(powered_tail_tracking) is not bool or (powered_capture_request is not None and not settings.powered_reference_capture):
        raise ValueError("invalid_powered_reference_capture_route")
    powered_selected = bool(settings.powered_reference_capture and unavailable and powered_capture_request is not None
        and not reference_capture_closed and (powered_capture_request.active or powered_tail_tracking))
    capture = bool(settings.low_pressure_reference_capture and unavailable and reference_capture_active and not powered_selected)
    energy_descent_active = bool(settings.energy_terminal_descent and unavailable and phase == "booster_landing_burn"
        and (not settings.planned_capture_impulse_compensation or observed["altitude_m"] <= 10000.))
    if powered_selected:
        if (phase != "booster_entry_coast" or coast_started_s is None
                or powered_capture_request.coast_started_s != coast_started_s or observed["dynamic_pressure_pa"] > 50.):
            raise ValueError("powered_reference_capture_outside_original_low_pressure_coast")
        from .starship_booster_powered_capture import powered_capture_control
        command, diagnostic = powered_capture_control(state, craft, profile, powered_capture_request,
            tail_tracking=powered_tail_tracking)
        mode = "powered_reference_capture" if powered_capture_request.active else "powered_reference_shutdown_tail"
        active, tail = True, powered_tail_tracking
    elif capture:
        if observed["dynamic_pressure_pa"] > 50. or tvc_capacity > jet_capacity or coast_started_s is None:
            raise ValueError("reference_capture_outside_low_pressure_weak_tail_scope")
        if settings.residual_authority_capture:
            from .starship_booster_residual_capture import residual_capture_control
            command, diagnostic = residual_capture_control(state, craft, profile, coast_started_s=coast_started_s,
                control_interval_s=control_interval_s, reference=capture_reference)
            mode = "low_pressure_residual_capture"
        else:
            from .starship_booster_lowq_capture import capture_control
            command, diagnostic = capture_control(state, craft, profile, coast_started_s=coast_started_s,
                control_interval_s=control_interval_s, reference=capture_reference)
            mode = "low_pressure_reference_capture"
    elif active:
        from .starship_booster_control import control_with_measured_tvc
        command, diagnostic = control_with_measured_tvc(state, craft, target, throttle, count, profile,
            use_flaps=use_flaps, recenter_uncommanded_main=not tail)
        mode = ("measured_tvc_shutdown_tail" if tail else "measured_tvc_recentered_coast"
                if phase == "booster_entry_coast" else "measured_tvc_available_main_control")
    else:
        from .starship_sixdof_mission import control
        command, diagnostic = control(state, craft, target, throttle, count, profile, use_flaps=use_flaps)
        mode = "legacy_degraded_control" if unavailable else "legacy_healthy_control"
    if settings.powered_reference_capture:
        uncommanded_tvc_capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
            *math.sin(engine.max_gimbal_rad) for engine, actual, load, target_command in
            zip(craft.engines, state.engine_states, observed["engine_loads"], command.engines)
            if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0
            and not (target_command.enabled and target_command.throttle > 0)))
        active = bool(powered_selected or unavailable and uncommanded_tvc_capacity > jet_capacity)
    diagnostic["booster_terminal_recovery"] = {"schema": "missionos.booster_terminal_recovery_control.v1",
        "policy_id": POLICY_ID, "settings": settings.record(), "mode": mode,
        "availability_control_active": active, "actual_unavailable_main_indices": unavailable,
        "activation_source": "actual_main_engine_availability", "current_main_thrust_n": main_thrust,
        "current_gimballed_main_torque_capacity_nm": tvc_capacity, "physical_rcs_pair_torque_nm": jet_capacity,
        "recenter_uncommanded_main": bool(active and not tail), "shutdown_tail_tvc_required": tail,
        "shutdown_tail_tvc_criterion": ("explicit_bounded_reference_pulse_or_current_capacity_shutdown_tail"
            if powered_selected else "actual_gimballed_main_capacity_exceeds_physical_rcs_pair"),
        "legacy_guidance_preserved": not capture and not powered_selected and not energy_descent_active
            and not (settings.planned_capture_impulse_compensation and unavailable), "finite_fin_allocator_added": False,
        "physics_limits_changed": False, "actual_state_assigned": False, "model_calls": 0,
        **({"shutdown_tail_only": True,
            "uncommanded_gimballed_main_torque_capacity_nm": uncommanded_tvc_capacity,
            "tail_allocation_selected": (bool(unavailable and uncommanded_tvc_capacity > jet_capacity)
                if settings.powered_reference_capture else active)} if settings.shutdown_tail_only else {}),
        **({"low_pressure_reference_capture": True,
            "reference_capture_active": capture, "reference_capture_closed": reference_capture_closed,
            "coast_started_s": coast_started_s,
            "legacy_requested_main_count": count, "legacy_requested_main_throttle": throttle,
            "control_interval_s": control_interval_s,
            "handoff_reason": "dynamic_pressure_exceeded_50pa" if reference_capture_closed else None}
            if settings.low_pressure_reference_capture else {}),
        **({"powered_reference_capture": True, "powered_reference_control_selected": powered_selected,
            "powered_reference_tail_tracking": bool(powered_selected and powered_tail_tracking),
            "powered_reference_capture_request": None if powered_capture_request is None else powered_capture_request.record()}
            if settings.powered_reference_capture else {}),
        **({"residual_authority_capture": True} if settings.residual_authority_capture else {}),
        **({"energy_terminal_descent": True, "energy_terminal_descent_active": energy_descent_active}
            if settings.energy_terminal_descent else {}),
        **({"planned_capture_impulse_compensation": True} if settings.planned_capture_impulse_compensation else {}),
        **({"terminal_horizontal_braking": True} if settings.terminal_horizontal_braking else {})}
    return command, diagnostic


def terminal_physical_booster(profile, separation_state, settings, *, return_sites,
                              splashdown_goal, duration_s=1200., on_tick=None):
    """Explicit exact-separation suffix; execution needs a registered caller."""
    validate_terminal_scope(profile, settings, return_sites, splashdown_goal)
    if (type(duration_s) not in (int, float) or isinstance(duration_s, bool) or not math.isfinite(duration_s)
            or not 0 < duration_s <= 2000. or on_tick is not None and not callable(on_tick)):
        raise ValueError("invalid_terminal_recovery_trial_scope")
    from .starship_sixdof_booster import _simulate_booster_steps
    iterator = _simulate_booster_steps(profile, separation_state, duration_s=duration_s,
        guidance_policy=POLICY_ID, terminal_recovery_settings=settings,
        return_sites=return_sites, splashdown_goal=splashdown_goal)
    peak, highest = None, -math.inf
    try:
        while True:
            try:
                tick = next(iterator)
            except StopIteration as done:
                result = done.value
                break
            altitude = env.ecef_to_geodetic(tick.state.r_eci_m)[2]
            if altitude > highest:
                peak, highest = asdict(tick.state), altitude
            if on_tick is not None:
                on_tick(tick)
    finally:
        iterator.close()
    if env.ecef_to_geodetic(tuple(result["final_state"]["r_eci_m"]))[2] > highest:
        peak = result["final_state"]
    result["runner_booster_peak_witness"] = {"state": peak,
        "origin": "all_yielded_physics_ticks_and_final_state; no extra plant step",
        "not_independent_physics_reexecution": True}
    return result

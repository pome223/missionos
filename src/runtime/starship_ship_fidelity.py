"""Opt-in Stage2d continuation of a source-bound post-insertion plant state.

This is a physical-model experiment, not a mission dispatch or qualification.
It retains all26 payloads and the recorded unavailable RVac. No observed real
flight epoch, deployment, eleven-second deorbit, or hardware fidelity is inferred.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import re

from . import starship_physics as env, starship_sixdof as dyn

POLICY_ID = "ship_entry_fidelity_v1"
ORIGIN_SCHEMA = "missionos.ship_post_insertion_origin.v1"


def _encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


def _read(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_ship_origin_json_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda raw: (_ for _ in ()).throw(ValueError("nonfinite_ship_origin")))
    _encode(value)
    return value


@dataclass(frozen=True)
class ShipFidelitySettings:
    entry_alpha_deg: float
    flip_altitude_m: float

    def __post_init__(self):
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (self.entry_alpha_deg, self.flip_altitude_m))
                or self.entry_alpha_deg not in (65., 70., 75.) or self.flip_altitude_m not in (500., 800.)):
            raise ValueError("invalid_registered_ship_fidelity_settings")

    def record(self):
        return asdict(self)


@dataclass(frozen=True)
class PostInsertionScreenOrigin:
    source_record_sha256: str
    _source_json: str = field(repr=False)
    _profile_json: str = field(repr=False)

    def __post_init__(self):
        if (type(self.source_record_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.source_record_sha256)
                or type(self._source_json) is not str or type(self._profile_json) is not str
                or sha256(self._source_json.encode()).hexdigest() != self.source_record_sha256):
            raise ValueError("ship_origin_source_hash_mismatch")
        _validate_source(_read(self._source_json), _read(self._profile_json))

    @property
    def time_s(self):
        return _read(self._source_json)["final_state"]["time_s"]

    @property
    def retained_count(self):
        return 26

    def record(self):
        source = _read(self._source_json)
        return {"schema": ORIGIN_SCHEMA, "basis": "actual_post_insertion_screen_continuation_v1",
            "source_record_sha256": self.source_record_sha256,
            "profile_sha256": sha256(self._profile_json.encode()).hexdigest(),
            "source_phase": source["outcome"]["phase"], "retained_count": 26,
            "state": deepcopy(source["final_state"]), "vehicle": deepcopy(source["final_vehicle"]),
            "qualified_return": False, "mission_authority_granted": False, "physical_execution": False}


def _validate_source(source, profile):
    if type(source) is not dict or type(profile) is not dict:
        raise ValueError("ship_origin_requires_source_record_and_profile")
    outcome, screen = source.get("outcome", {}), source.get("ascent_screen", {})
    samples = source.get("samples")
    if (type(outcome) is not dict or type(screen) is not dict or type(samples) is not list or not samples
            or type(samples[-1]) is not dict or type(source.get("final_state")) is not dict
            or type(source.get("final_vehicle")) is not dict):
        raise ValueError("ship_origin_requires_complete_saved_state")
    if (source.get("scenario") != "launch" or outcome.get("phase") != "ascent_screen_shutdown"
            or outcome.get("termination") != "insertion_gate_reached" or outcome.get("orbit_gate_reached") is not True
            or outcome.get("payload_released_count") != 0 or source.get("satellites") != []
            or profile.get("payload", {}).get("count") != 26
            or screen.get("policy") != "guidance_outcome_v3" or screen.get("insertion_gate_reached") is not True
            or screen.get("mission_dispatch") is not False or screen.get("return_qualified") is not False):
        raise ValueError("unsupported_post_insertion_screen_origin")
    state = dyn.state_from_dict(source["final_state"])
    vehicle = dyn.vehicle_from_dict(source["final_vehicle"])
    observed = dyn.observe(state, vehicle)
    orbit = env.orbital_elements(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    if (state.time_s != 1537. or orbit["status"] != "bound"
            or orbit["perigee_altitude_m"] < profile["guidance"]["target_perigee_m"]
            or state.propellant_kg <= profile["ship"]["return_reserve_kg"]
            or observed["altitude_m"] < 150000.
            or vehicle.dry_mass_kg != profile["ship"]["dry_mass_kg"]+26*profile["payload"]["mass_each_kg"]
            or [engine.name for engine in vehicle.engines[:6]] != [f"ship_main_{i}" for i in range(6)]
            or [engine.name for engine, actual in zip(vehicle.engines, state.engine_states) if not actual.available] != ["ship_main_3"]
            or samples[-1].get("phase") != "ascent_screen_shutdown"
            or any(samples[-1].get(key) != value for key, value in source["final_state"].items())):
        raise ValueError("post_insertion_actual_state_or_inventory_not_supported")
    return state, vehicle


def post_insertion_screen_origin(record, profile, *, record_sha256, raw_record_bytes=None):
    """Bind the actual serialized source; constructing it advances no state."""
    raw = _encode(record) if raw_record_bytes is None else raw_record_bytes
    if type(raw) is not bytes or _read(raw) != record:
        raise ValueError("ship_origin_raw_record_differs")
    return PostInsertionScreenOrigin(record_sha256, raw.decode("utf-8"), _encode(profile).decode("utf-8"))


def ship_fidelity_steps(origin, profile, *, settings=None, return_time_s=None, duration_s=6000., dt_scale=1.,
                        integration_substeps=None):
    """Prepare a generator of actual control-tick states and a saved return.

    Merely preparing the generator does not integrate. Iteration consumes the
    separately registered physical-model trial. No providers or approvals run.
    Explicit integration_substeps refines a held physical command without
    changing control/allocator periods. Omitted preserves the old dt_scale
    experiment, which changes both those periods and physical stepping.
    """
    if (type(origin) is not PostInsertionScreenOrigin
            or settings is not None and type(settings) is not ShipFidelitySettings
            or origin._profile_json.encode() != _encode(profile)
            or type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 < duration_s <= 6000.
            or type(dt_scale) not in (int, float) or dt_scale not in (1., .5)
            or integration_substeps is not None and
               (type(integration_substeps) is not int or integration_substeps not in (1, 2, 4))
            or integration_substeps is not None and integration_substeps > 1 and dt_scale != 1.):
        raise ValueError("invalid_ship_fidelity_continuation_scope")
    requested = origin.time_s+60.
    if return_time_s is not None and (type(return_time_s) not in (int, float) or return_time_s != requested):
        raise ValueError("ship_fidelity_requires_registered_return_request_time")
    if duration_s <= 60.:
        raise ValueError("ship_fidelity_horizon_precedes_return_request")
    return _steps(origin, deepcopy(profile), settings, requested, duration_s, dt_scale, integration_substeps)


def _steps(origin, profile, settings, return_time_s, duration_s, dt_scale, integration_substeps):
    from .starship_concurrent import PhysicsTick
    from .starship_retained_return import TRIMMED_POLICY_ID
    from .starship_ship_return import ReturnController, control_interval, record_sample, advance_plant
    from .starship_sixdof_mission import _sample

    before = origin.record()
    if dt_scale != 1.:
        # The convergence variant changes the integration and finite allocation
        # step together; gains, loads, geometry and the source profile stay bound.
        profile = deepcopy(profile)
        for key in ("powered_dt_s", "coast_dt_s"):
            profile["integration"][key] *= dt_scale
    s = dyn.state_from_dict(before["state"])
    v = dyn.vehicle_from_dict(before["vehicle"])
    controller = ReturnController(TRIMMED_POLICY_ID, return_time_s, 0,
        allocation_enabled=True, registered=False)
    start, end = s.time_s, s.time_s+duration_s
    termination, contact, count = "time_limit", None, 0
    samples, events = [_sample(s, v, before["source_phase"])], []
    next_sample, last_tick = start, None
    command_witnesses = {"deorbit": None, "deorbit_cutoff": None, "flip": None}
    load_witnesses = {"deorbit": None, "landing": None}
    pending_commands = set()

    def event(name, detail="", **fields):
        phases = {"deorbit_ignition_command": "deorbit", "deorbit_cutoff_command": "deorbit_cutoff",
                  "flip_and_landing_command": "flip"}
        if name in phases:
            fields.setdefault("state", asdict(s))
            pending_commands.add(phases[name])
        events.append({"time_s": s.time_s, "event": name, "detail": detail, **fields})

    def loads(observed):
        main_loads = [row["thrust_n"] for engine, row in zip(v.engines, observed["engine_loads"]) if "main" in engine.name]
        for kind, request_kind in (("deorbit", "deorbit"), ("landing", "flip")):
            request = command_witnesses[request_kind]
            if request is not None and load_witnesses[kind] is None and any(value > 1000. for value in main_loads[:3]):
                load_witnesses[kind] = {"time_s": s.time_s, "state": asdict(s),
                    "main_engine_thrust_n": main_loads, "positive_main_count": sum(value > 1000. for value in main_loads),
                    "threshold_n": 1000., "meaning": "first_at_or_after_request_positive_observed_load_not_proven_ignition"}

    event("post_insertion_return_continuation", "Actual saved insertion state and vehicle;26 retained, no deployment or return qualification",
        source_record_sha256=origin.source_record_sha256, source_phase=before["source_phase"],
        controller_phase=controller.phase, state_before=before["state"], state_after=asdict(s))
    while s.time_s < end-1e-8:
        last_tick = s.time_s
        yield PhysicsTick("ship", s.time_s, controller.phase, s)
        observed = dyn.observe(s, v)
        loads(observed)
        step = controller.update(s, v, profile, samples, event, fidelity_settings=settings)
        s = step.state
        if step.transition_only:
            continue
        command, diagnostic = controller.command(step, v, profile, observed["altitude_m"])
        for name in tuple(pending_commands):
            if command_witnesses[name] is None:
                command_witnesses[name] = {"time_s": s.time_s, "state": asdict(s), "command": asdict(command),
                    "requested_main_count": step.count, "requested_throttle": step.throttle}
            pending_commands.remove(name)
        next_sample = record_sample(s, v, controller.phase, command, diagnostic, samples, next_sample, profile)
        if env.norm(s.omega_body_rad_s) > 5.:
            termination = "angular_rate_envelope_exceeded"
            event(termination, "Unchanged development rate envelope; no structural survival inferred")
            break
        dt = min(end-s.time_s, control_interval(profile, step.throttle, observed["altitude_m"]))
        if s.time_s < return_time_s < s.time_s+dt:
            dt = return_time_s-s.time_s
        try:
            integration_options = {} if integration_substeps is None else {"integration_substeps": integration_substeps}
            s, receipt = advance_plant(s, v, command, dt, profile["geometry"]["ship_length_m"],
                profile["geometry"]["radius_m"], observed["altitude_m"], observed["air_speed_mps"], **integration_options)
            count += 1
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5. else "low_speed_surface_contact"
                event(termination, "Actual finite-geometry contact; no ocean or structural response", contact=receipt)
                break
        except (ValueError, OverflowError, FloatingPointError) as error:
            termination = "numerical_failure"
            event(termination, f"{type(error).__name__}: {error}; last finite state retained")
            break
    if last_tick != s.time_s:
        yield PhysicsTick("ship", s.time_s, controller.phase, s)
    final = _sample(s, v, controller.phase)
    loads(dyn.observe(s, v))
    final["contact"] = contact is not None
    samples.append(final)
    if termination == "time_limit":
        event(termination, "Registered continuation horizon reached; no recovery inferred")
    if origin.record() != before:
        raise AssertionError("ship_fidelity_mutated_origin")
    return {"scenario": "ship_fidelity_return", "origin": before, "samples": samples, "events": events,
        "initial_state": samples[0], "final_state": asdict(s), "final_vehicle": asdict(v),
        "return_controller": controller.to_dict(), "retained_return": controller.record,
        "outcome": {"termination": termination, "phase": controller.phase, "duration_s": s.time_s-start,
            "start_time_s": start, "end_time_s": s.time_s, "integration_steps": count,
            "contact_receipt": contact, "final_altitude_m": final["altitude_m"],
            "final_ground_speed_mps": final["ground_speed_mps"], "six_dof_integrated": True,
            "attitude_prescribed": False, "starship_vehicle_validated": False, "payload_released_count": 0},
        "ship_fidelity": {"schema": "missionos.starship_ship_fidelity.v1",
            "policy_id": POLICY_ID if settings is not None else "baseline_trimmed_return",
            "settings": settings.record() if settings is not None else None, "dt_scale": dt_scale,
            "effective_integration": {key: profile["integration"][key] for key in ("powered_dt_s", "coast_dt_s")},
            "allocation_interval_uses_effective_nominal_control_step": True,
            **({"numerical_clock_policy": "fixed_control_hold_refined_plant_v1",
                "integration_substeps": integration_substeps,
                "controller_polling_uses_control_hold_period": True,
                "allocator_horizon_uses_control_hold_period": True,
                "command_held_during_integration_substeps": True,
                "integration_steps_counts_control_holds": True,
                "integration_refinement_is_error_bound": False} if integration_substeps is not None else {}),
            "return_request_time_s": return_time_s, "retained_count": 26, "source_phase": before["source_phase"],
            "command_witnesses": command_witnesses, "first_load_witnesses": load_witnesses,
            "qualification_refreshed": False, "mission_authority_granted": False, "physical_execution": False,
            "deorbit_duration_forced": False, "reported_eleven_seconds_is_planned_not_flown": True}}

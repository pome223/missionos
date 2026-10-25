"""Offline assumed/actual capability grid; never runtime return admission.

The return phase machine, finite control and physical stepping are shared with
stage 1. Only the experimental driver separates its model and plant vehicles.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import time
from unittest.mock import patch

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_ship_fault_screen as screen, starship_sixdof_mission as mission
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_return_prediction import validate_origin
from .starship_ship_return import advance_plant, control_interval, record_sample


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def capacity_vehicle(nominal, factor):
    if type(factor) not in (int, float) or factor not in (1., .8, .6):
        raise ValueError("unregistered_capability_factor")
    return replace(nominal, engines=tuple(
        replace(e, max_thrust_n=e.max_thrust_n*factor) if i < 3 else e
        for i, e in enumerate(nominal.engines)))


def actual_fault(factor):
    if factor == 1.:
        return None
    if factor not in (.8, .6):
        raise ValueError("unregistered_actual_fault")
    return {"id": f"all_landing_thrust_{round(factor*100)}pct",
            "kind": "landing_engine_derate", "engine_index": None,
            "thrust_factor": factor}


class BoundaryAudit:
    """Observe and reject actual-vehicle injection at each control boundary."""

    def __init__(self, nominal, model, plant):
        if model is plant:
            raise ValueError("model_and_plant_must_be_distinct")
        self.plant_identity = id(plant)
        self.assumed_limits = [e.max_thrust_n for e in model.engines]
        self.actual_limits = [e.max_thrust_n for e in plant.engines]
        self.rows = []
        self.nominal_limits = [e.max_thrust_n for e in nominal.engines]

    def inspect(self, boundary, state, craft, profile=None):
        limits = [e.max_thrust_n for e in craft.engines]
        is_plant = id(craft) == self.plant_identity
        row = {"time_s": state.time_s, "boundary": boundary,
               "engine_limits_n": limits, "is_actual_plant_object": is_plant,
               "profile_main_limit_n": profile["ship"]["engine_thrust_n"] if profile is not None else None}
        self.rows.append(row)
        if boundary == "plant":
            if not is_plant or limits != self.actual_limits:
                raise ValueError("wrong_vehicle_at_physical_boundary")
        elif is_plant or limits != self.assumed_limits:
            raise ValueError("actual_capability_leaked_into_control")
        if profile is not None and row["profile_main_limit_n"] != self.assumed_limits[0]:
            raise ValueError("wrong_capability_in_control_profile")

    def to_dict(self):
        return {"schema": "missionos.ship_control_capability_audit.v1",
                "assumed_engine_limits_n": self.assumed_limits,
                "actual_engine_limits_n": self.actual_limits,
                "all_boundary_calls": self.rows,
                "trace_sha256": sha256(encoded(self.rows)).hexdigest()}


class SeparatedController:
    """The controller receives only a model vehicle; it never receives a plant."""

    def __init__(self, model, profile, return_time_s, released_count, audit):
        self.model, self.profile, self.audit = model, profile, audit
        self.controller = screen.AvailableCapacityController(
            TRIMMED_POLICY_ID, return_time_s, released_count, allocation_enabled=True)
        self.original_profile = screen.capability_profile
        self.original_control = mission.control

    @property
    def phase(self):
        return self.controller.phase

    def _profile(self, profile, craft):
        result = self.original_profile(profile, craft)
        self.audit.inspect("capability_profile", self.current_state, craft, result)
        return result

    def _control(self, state, craft, *args, **kwargs):
        self.audit.inspect("tvc_control", state, craft, args[3])
        return self.original_control(state, craft, *args, **kwargs)

    def update(self, state, samples, event):
        self.current_state = state
        self.audit.inspect("update", state, self.model)
        with patch.object(screen, "capability_profile", self._profile):
            return self.controller.update(state, self.model, self.profile, samples, event)

    def command(self, step, altitude):
        self.current_state = step.state
        self.audit.inspect("command", step.state, self.model)
        with ExitStack() as stack:
            stack.enter_context(patch.object(screen, "capability_profile", self._profile))
            stack.enter_context(patch.object(mission, "control", self._control))
            command, diagnostics = self.controller.command(step, self.model, self.profile, altitude)
        allocation = diagnostics["fault_return_allocation"]
        allocation["assumed_main_engine_thrust_limit_n"] = allocation.pop("actual_main_engine_thrust_limit_n")
        return command, diagnostics


def rollout(origin, profile, return_time_s, cell, *, wall_limit_s, cohort_deadline):
    before = deepcopy(origin)
    original, nominal = validate_origin(origin, profile, return_time_s, 3600.)
    fault = actual_fault(cell["actual_thrust_factor"])
    state, plant = screen.apply_fault(original, nominal, fault)
    # Always a separate object, including equal-capability diagonal rows.
    plant = replace(plant)
    model = capacity_vehicle(nominal, cell["assumed_thrust_factor"])
    audit = BoundaryAudit(nominal, model, plant)
    controller = SeparatedController(model, profile, return_time_s,
        profile["payload"]["count"]-origin["retained_count"], audit)
    initial = asdict(state)
    started = time.monotonic()
    deadline = min(started+wall_limit_s, cohort_deadline)
    end = state.time_s+3600.
    samples, events = [], []
    contact = None
    termination, error, steps = "time_limit", None, 0
    next_sample = state.time_s

    def event(name, detail="", **data):
        events.append({"time_s": state.time_s, "event": name, "detail": detail, **data})

    try:
        while state.time_s < end-1e-8:
            if time.monotonic() >= deadline:
                termination = "execution_wall_budget"
                break
            obs = dyn.observe(state, plant)
            altitude = obs["altitude_m"]
            step = controller.update(state, samples, event)
            state = step.state
            if step.transition_only:
                continue
            command, diagnostic = controller.command(step, altitude)
            next_sample = record_sample(state, plant, controller.phase, command, diagnostic,
                                       samples, next_sample, profile)
            if env.norm(state.omega_body_rad_s) > 5:
                termination = "angular_rate_envelope_exceeded"
                break
            dt = min(end-state.time_s, control_interval(profile, step.throttle, altitude))
            audit.inspect("plant", state, plant)
            state, receipt = advance_plant(state, plant, command, dt,
                profile["geometry"]["ship_length_m"], profile["geometry"]["radius_m"], altitude, obs["air_speed_mps"])
            steps += 1
            if receipt is not None and receipt["contact"]:
                contact = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                break
    except (ValueError, OverflowError, FloatingPointError, InterruptedError) as exc:
        termination, error = "numerical_or_control_failure", {"type": type(exc).__name__, "detail": str(exc)}
    samples.append(mission._sample(state, plant, controller.phase))
    if before != origin:
        raise AssertionError("mutated_origin")
    return {"schema": "missionos.ship_fault_stage1_rollout.v1", "origin": before,
        "fault": fault, "faulted_initial_state": initial,
        "actual_engine_limits_n": [e.max_thrust_n for e in plant.engines],
        "candidate": "available_capacity_return_v1", "return_time_s": return_time_s,
        "events": events, "samples": samples, "final_state": asdict(state),
        "outcome": {"termination": termination, "contact_receipt": contact, "integration_steps": steps},
        "error": error, "wall_time_s": time.monotonic()-started,
        "fault_domain_status": "unverified", "no_fault_certificate_used_for_admission": False,
        "runtime_admission": False, "model_calls": 0, "physical_execution": False,
        "assumed_actual_cell": deepcopy(cell), "control_capability_audit": audit.to_dict()}


def verify_record(record, origin, profile, cell, limits):
    """Reconstruct both capabilities independently, and check every audited call."""
    result = screen.verify_record(record, origin, profile, actual_fault(cell["actual_thrust_factor"]),
                                  "available_capacity_return_v1", limits)
    checks = result["checks"]
    try:
        nominal = mission.vehicle(profile, payload_count=origin["retained_count"])
        assumed = [e.max_thrust_n*cell["assumed_thrust_factor"] if i < 3 else e.max_thrust_n
                   for i, e in enumerate(nominal.engines)]
        actual = [e.max_thrust_n*cell["actual_thrust_factor"] if i < 3 else e.max_thrust_n
                  for i, e in enumerate(nominal.engines)]
        audit = record["control_capability_audit"]
        rows = audit["all_boundary_calls"]
        controls = [r for r in rows if r["boundary"] != "plant"]
        plants = [r for r in rows if r["boundary"] == "plant"]
        counts = {k: sum(r["boundary"] == k for r in rows) for k in
                  ("update", "command", "capability_profile", "tvc_control", "plant")}
        checks["assumed_actual_cell_bound"] = record["assumed_actual_cell"] == cell
        checks["control_limits_bound"] = audit["assumed_engine_limits_n"] == assumed
        checks["plant_limits_bound"] = audit["actual_engine_limits_n"] == actual
        checks["complete_control_trace_hash"] = audit["trace_sha256"] == sha256(encoded(rows)).hexdigest()
        checks["all_control_calls_use_assumed_limits"] = bool(controls) and all(
            r["engine_limits_n"] == assumed and r["is_actual_plant_object"] is False for r in controls)
        checks["all_profiles_use_assumed_limits"] = all(
            r["profile_main_limit_n"] == assumed[0] for r in rows
            if r["boundary"] in ("capability_profile", "tvc_control"))
        checks["all_physical_steps_use_actual_plant"] = bool(plants) and all(
            r["engine_limits_n"] == actual and r["is_actual_plant_object"] is True for r in plants)
        steps = record["outcome"]["integration_steps"]
        checks["complete_boundary_coverage"] = (
            counts["plant"] == counts["command"] == counts["tvc_control"] == steps
            and counts["update"] >= steps
            and counts["capability_profile"] == counts["update"]+counts["command"]
            and len(rows) == sum(counts.values()))
        checks["control_trace_time_ordered"] = all(a["time_s"] <= b["time_s"] for a, b in zip(rows, rows[1:]))
        position, routed_steps, ordered = 0, 0, True
        while position < len(rows):
            block = rows[position:position+2]
            if [r["boundary"] for r in block] != ["update", "capability_profile"]:
                ordered = False
                break
            position += 2
            if position < len(rows) and rows[position]["boundary"] == "command":
                block += rows[position:position+4]
                if [r["boundary"] for r in block[2:]] != ["command", "capability_profile", "tvc_control", "plant"]:
                    ordered = False
                    break
                position += 4
                routed_steps += 1
            if any(r["time_s"] != block[0]["time_s"] for r in block):
                ordered = False
                break
        checks["every_step_has_ordered_model_to_plant_route"] = ordered and routed_steps == steps
        result["boundary_call_counts"] = counts
        # ReturnController emits modeled witnesses; the driver emits physical
        # samples. Both are retained, and neither may masquerade as the other.
        # These marker fields come from the unchanged shared controller source.
        import math
        update_times = {r["time_s"] for r in rows if r["boundary"] == "update"}
        provenance_valid, thrust_valid = True, True
        witness_count = 0
        for sample in record["samples"]:
            retained = sample.get("retained_return")
            trim = sample.get("entry_trim")
            witness = retained is not None or trim is not None
            if retained not in (None, "activation", "previous", "trigger") or trim not in (None, "preparation"):
                provenance_valid = False
            if witness:
                witness_count += 1
                expected_phase = "orbital_coast" if retained == "activation" else "ballistic_return"
                if sample["time_s"] not in update_times or sample["phase"] != expected_phase:
                    provenance_valid = False
            expected_limits = assumed if witness else actual
            for i in range(6):
                engine = sample["engine_states"][i]
                expected_thrust = expected_limits[i]*engine["throttle"] if engine["available"] and sample["propellant_kg"] > 0 else 0.
                thrust_valid &= math.isclose(sample["main_engine_thrust_n"][i], expected_thrust,
                                              rel_tol=1e-9, abs_tol=1e-6)
        result["legacy_undifferentiated_thrust_check_passed"] = checks.get("observed_main_thrust_matches_capability")
        checks["observed_main_thrust_matches_capability"] = thrust_valid
        checks["controller_witness_provenance_valid"] = provenance_valid
        result["sample_provenance"] = {"assumed_control_model_witnesses": witness_count,
            "actual_plant_samples": len(record["samples"])-witness_count,
            "basis": "shared controller marker, phase and audited update time; all other samples are actual-plant samples"}
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        checks["control_trace_valid"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["record_valid"] = all(checks.values())
    contact = result.get("contact")
    result["contact_limits_met"] = bool(result["record_valid"] and contact
        and all(contact[k] <= limits[k] for k in ("speed_mps", "tilt_deg", "body_rate_rad_s"))
        and contact["reserve_kg"] >= limits["reserve_kg"])
    return result


def recorded_physics(record):
    """Exact old recorded values; audit/wall metadata and diagnostic naming excluded.

    This compares saved data, not the unsaved integration trajectory.
    """
    value = {k: deepcopy(record[k]) for k in (
        "origin", "fault", "faulted_initial_state", "actual_engine_limits_n", "candidate",
        "return_time_s", "events", "samples", "final_state", "outcome", "error")}
    for sample in value["samples"]:
        # Only the diagnostic label changed, not its numerical value.
        allocation = sample.get("controller", {}).get("fault_return_allocation")
        if allocation and "assumed_main_engine_thrust_limit_n" in allocation:
            allocation["actual_main_engine_thrust_limit_n"] = allocation.pop("assumed_main_engine_thrust_limit_n")
    return encoded(value)

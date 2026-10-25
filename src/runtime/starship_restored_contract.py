"""Immutable, unqualified evidence boundary for the restored physical mission.

Input schema ``missionos.starship_restored_handoff_record.v1`` has exactly:
``source_sha256, profile_sha256, experimental_mission_ascent,
booster_control_settings, phase, final_state, final_vehicle, state_history,
inventory, schema``. History rows are ``{state, retained_count}``. Inventory is
``{schema,total_count,released_count,retained_count,generation,sequencer_state,
pending_action,observed_time_s,release_receipts}``; receipts are the actual
``sixdof_payload_release.v1`` objects, including both parent states and child.

The caller supplies the host-frozen expected source manifest separately. Its
equality is provenance evidence until an outer worker authenticates its receipt;
this pure factory proves no host signature, operator identity, unsampled flight
dynamics, independent physical model, approval, dispatch or qualification.
"""
from __future__ import annotations

from dataclasses import InitVar, asdict, dataclass, field
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re

from . import starship_physics as env, starship_sixdof as dyn

PROFILE_SHA256 = "4a0bebe69c97978daeabe7f16a4b30a1b69864a75668217106b1ca0ef4439df5"
RECORD_SCHEMA = "missionos.starship_restored_handoff_record.v1"
ORIGIN_SCHEMA = "missionos.starship_restored_actual_origin.v1"
INVENTORY_SCHEMA = "missionos.starship_restored_inventory.v1"
CRITICAL_SOURCES = frozenset({
    "src/runtime/starship_physics.py", "src/runtime/starship_sixdof.py",
    "src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof_booster.py",
    "src/runtime/starship_state_mission.py", "src/runtime/starship_ship_return.py",
    "src/runtime/starship_retained_return.py", "src/runtime/starship_sixdof_separation.py",
    "src/runtime/starship_booster_terminal_control.py", "src/runtime/starship_return_sites.py",
    "src/runtime/starship_restored_contract.py",
    "examples/spaceflight/starship-sixdof-profile.json",
    "examples/spaceflight/starship-return-sites-model-test.json"})
STATE_KEYS = frozenset({"time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci",
    "omega_body_rad_s", "propellant_kg", "engine_states", "flap_angles_rad"})
MAXIMUM_SIMULATION_TIME_S = 16000.0


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("restored_value_is_not_finite_json") from error


def _read(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, "restored_duplicate_json_key")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("restored_nonfinite_json_" + value)
    _require(type(raw) is bytes and 0 < len(raw) <= 32 * 1024**2, "restored_raw_json_budget")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("restored_invalid_json") from error
    _encode(value)
    return value


def _same(a, b):
    return _encode(a) == _encode(b)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _manifest(value):
    _require(type(value) is dict and CRITICAL_SOURCES <= value.keys(), "restored_critical_sources_missing")
    for name, digest in value.items():
        path = PurePosixPath(name) if type(name) is str else None
        _require(path is not None and not path.is_absolute() and ".." not in path.parts
            and path.suffix in (".py", ".json", ".md") and _hash(digest), "restored_invalid_source_manifest")
    _require(value["examples/spaceflight/starship-sixdof-profile.json"] == PROFILE_SHA256,
        "restored_source_profile_pin_changed")


def _settings(metadata, control):
    from .starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
        AscentScreenSettings, AscentShutdown, StateInsertionOptions)
    from .starship_state_mission import ExperimentalStateMissionAscentSettings
    from .starship_booster_terminal_control import BoosterTerminalRecoverySettings
    expected = []
    for outages in ((), (AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.))):
        expected.append(ExperimentalStateMissionAscentSettings(AscentScreenSettings(
            policy="state_insertion_v7", shutdowns=outages,
            objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
            control_options=AscentControlOptions(True, False), insertion_options=StateInsertionOptions())).record())
    selected = next((row for row in expected if _same(metadata.get("settings"), row)), None)
    _require(metadata.get("schema") == "missionos.experimental_state_mission_ascent.v1"
        and selected is not None, "restored_requires_frozen_v7_source_settings")
    wanted = BoosterTerminalRecoverySettings(powered_terminal_alignment=False, shutdown_tail_only=True,
        low_pressure_reference_capture=True, powered_reference_capture=True, residual_authority_capture=True,
        energy_terminal_descent=True, planned_capture_impulse_compensation=True, terminal_horizontal_braking=True).record()
    _require(_same(control, wanted), "restored_requires_terminal_braking_v11_settings")
    return selected


def _orbit(state):
    return env.orbital_elements(env.State3D(state["time_s"], tuple(state["r_eci_m"]),
        tuple(state["v_eci_mps"]), state["propellant_kg"]))


def _state(raw, craft, outages):
    _require(type(raw) is dict and set(raw) == STATE_KEYS, "restored_complete_eight_field_state_required")
    _require(_number(raw["time_s"]) and 0 <= raw["time_s"] < MAXIMUM_SIMULATION_TIME_S
        and _number(raw["propellant_kg"]) and 0 <= raw["propellant_kg"] <= craft.propellant_capacity_kg,
        "restored_invalid_state_scalar")
    for key, count in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(type(raw[key]) is list and len(raw[key]) == count and all(_number(x) for x in raw[key]),
            "restored_invalid_state_vector")
    _require(abs(sum(x*x for x in raw["q_body_to_eci"])-1.) <= 1e-8
        and env.norm(raw["r_eci_m"]) > env.EARTH_EQUATORIAL_RADIUS_M,
        "restored_invalid_attitude_or_position")
    _require(type(raw["engine_states"]) is list and len(raw["engine_states"]) == len(craft.engines)
        and type(raw["flap_angles_rad"]) is list and len(raw["flap_angles_rad"]) == len(craft.aero_panels),
        "restored_full_actuator_inventory_required")
    for engine, actual in zip(craft.engines, raw["engine_states"]):
        _require(type(actual) is dict and set(actual) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(actual["available"]) is bool
            and actual["available"] is (engine.name not in outages)
            and all(_number(actual[key]) for key in ("throttle", "gimbal_x_rad", "gimbal_y_rad")),
            "restored_engine_availability_reset_or_invalid_actuator")
    _require(all(_number(x) for x in raw["flap_angles_rad"]), "restored_nonfinite_flaps")
    state = dyn.state_from_dict(raw)
    dyn._validate_pair(state, craft, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines),
        state.flap_angles_rad))
    return state


def _difference(a, b):
    return env.add(a, env.scale(b, -1.))


def _release(receipt, profile, before_craft, after_craft, outages):
    """Stored-state rigid-release conservation, no release executor or plant."""
    _require(type(receipt) is dict and receipt.get("schema") == "missionos.sixdof_payload_release.v1"
        and receipt.get("velocity_target_inserted") is False and receipt.get("real_dispenser_validated") is False,
        "restored_invalid_release_receipt")
    before = _state(receipt["parent_before_state"], before_craft, outages)
    after = _state(receipt["parent_after_state"], after_craft, outages)
    child = receipt["child_state"]
    _require(type(child) is dict and set(child) == STATE_KEYS and child["time_s"] == before.time_s == after.time_s
        == receipt.get("time_s") and child["propellant_kg"] == 0
        and child["engine_states"] == child["flap_angles_rad"] == []
        and all(type(child[key]) is list and len(child[key]) == size and all(_number(x) for x in child[key])
            for key, size in (("r_eci_m",3),("v_eci_mps",3),("q_body_to_eci",4),("omega_body_rad_s",3))),
        "restored_invalid_child_state")
    _require(before.propellant_kg == after.propellant_kg
        and before.engine_states == after.engine_states and before.flap_angles_rad == after.flap_angles_rad
        and before.q_body_to_eci == after.q_body_to_eci == tuple(child["q_body_to_eci"]),
        "restored_release_reset_state_or_actuators")
    mass = profile["payload"]["mass_each_kg"]
    bp, ap = (dyn.mass_properties(craft, before.propellant_kg) for craft in (before_craft, after_craft))
    _require(receipt.get("child_mass_kg") == mass and abs(bp.mass_kg-ap.mass_kg-mass) <= 1e-5,
        "restored_retained_mass_does_not_follow_receipt")
    from .starship_sixdof_mission import payload_inertia
    tensor = payload_inertia(profile)
    _require(_same(receipt.get("child_inertia_kg_m2"), tensor), "restored_child_inertia_changed")
    child_position = (0.,0.,profile["payload"]["center_z_m"])
    _require(_same(receipt.get("child_position_body_m"), child_position)
        and _same(receipt.get("impulse_application_body_m"), child_position), "restored_release_geometry_changed")
    impulse = (ap.mass_kg*mass/(ap.mass_kg+mass)*profile["payload"]["release_speed_mps"],0.,0.)
    _require(env.norm(_difference(receipt["impulse_body_ns"], impulse)) <= 1e-6, "restored_release_impulse_changed")
    parent_offset = _difference(ap.com_body_m,bp.com_body_m)
    child_offset = _difference(child_position,bp.com_body_m)
    for actual, target in ((after.r_eci_m, env.add(before.r_eci_m,
            dyn.rotate(before.q_body_to_eci,parent_offset))),
            (child["r_eci_m"], env.add(before.r_eci_m,
            dyn.rotate(before.q_body_to_eci,child_offset)))):
        _require(env.norm(_difference(actual, target)) <= 1e-6, "restored_release_COM_position_changed")
    impulse_eci = dyn.rotate(before.q_body_to_eci,impulse)
    parent_velocity = env.add(env.add(before.v_eci_mps,
        dyn.rotate(before.q_body_to_eci,env.cross(before.omega_body_rad_s,parent_offset))),
        env.scale(impulse_eci,-1./ap.mass_kg))
    child_velocity = env.add(env.add(before.v_eci_mps,
        dyn.rotate(before.q_body_to_eci,env.cross(before.omega_body_rad_s,child_offset))),
        env.scale(impulse_eci,1./mass))
    _require(env.norm(_difference(after.v_eci_mps,parent_velocity)) <= 1e-8
        and env.norm(_difference(child["v_eci_mps"],child_velocity)) <= 1e-8,
        "restored_release_body_impulse_velocity_failed")
    parent_spin_delta = dyn._solve(ap.inertia_kg_m2,
        env.cross(_difference(child_position,ap.com_body_m),env.scale(impulse,-1.)))
    child_spin_delta = dyn._solve(tensor,env.cross(_difference(child_position,child_position),impulse))
    _require(env.norm(_difference(after.omega_body_rad_s,
        env.add(before.omega_body_rad_s,parent_spin_delta))) <= 1e-10
        and env.norm(_difference(child["omega_body_rad_s"],
        env.add(before.omega_body_rad_s,child_spin_delta))) <= 1e-10,
        "restored_release_body_torque_impulse_failed")
    for name, expected in (("parent_spin_delta_body_rad_s",parent_spin_delta),
                           ("child_spin_delta_body_rad_s",child_spin_delta)):
        if name in receipt:
            _require(type(receipt[name]) is list and len(receipt[name]) == 3
                and all(_number(x) for x in receipt[name])
                and env.norm(_difference(receipt[name],expected)) <= 1e-10,
                "restored_release_recorded_spin_delta_failed")
    pv, cv = _difference(after.v_eci_mps,before.v_eci_mps), _difference(child["v_eci_mps"],before.v_eci_mps)
    p_residual = env.add(env.scale(pv,ap.mass_kg),env.scale(cv,mass))
    def spin(inertia, omega):
        return dyn.rotate(before.q_body_to_eci, tuple(env.dot(row,omega) for row in inertia))
    h_before = spin(bp.inertia_kg_m2,before.omega_body_rad_s)
    h_after = env.add(spin(ap.inertia_kg_m2,after.omega_body_rad_s), spin(tensor,child["omega_body_rad_s"]))
    h_after = env.add(h_after,env.add(env.cross(_difference(after.r_eci_m,before.r_eci_m),env.scale(pv,ap.mass_kg)),
        env.cross(_difference(child["r_eci_m"],before.r_eci_m),env.scale(cv,mass))))
    _require(env.norm(p_residual) <= .1 and env.norm(_difference(h_after,h_before)) <= 1.,
        "restored_release_momentum_conservation_failed")
    return before, after


def _validate(record, profile, source):
    required = {"schema","source_sha256","profile_sha256","experimental_mission_ascent",
        "booster_control_settings","phase","final_state","final_vehicle","state_history","inventory"}
    _require(type(record) is dict and set(record) == required and record["schema"] == RECORD_SCHEMA,
        "restored_handoff_record_schema_or_fields")
    _require(_same(record["source_sha256"],source) and record["profile_sha256"] == PROFILE_SHA256,
        "restored_handoff_source_or_profile_mismatch")
    metadata = record["experimental_mission_ascent"]
    _require(type(metadata) is dict, "restored_missing_ascent_evidence")
    settings = _settings(metadata,record["booster_control_settings"])
    outages = {row["engine_name"] for row in settings["ascent"]["shutdowns"] if row["engine_name"].startswith("ship_")}
    from .starship_sixdof_mission import vehicle
    crafts = {n:vehicle(profile,payload_count=n) for n in range(27)}
    cutoff, settled = metadata["cutoff_measurement"], metadata["settled_measurement"]
    cs, ss = _state(cutoff["state"],crafts[26],outages), _state(settled["state"],crafts[26],outages)
    orbit, cutoff_orbit = _orbit(settled["state"]), _orbit(cutoff["state"])
    _require(cutoff.get("gate_reached") is True and settled.get("gate_reached") is True
        and cutoff_orbit["status"] == "bound" and cutoff_orbit["perigee_altitude_m"] >= 220000.
        and cutoff["time_s"] == cs.time_s and settled["time_s"] == ss.time_s
        and abs(ss.time_s-cs.time_s-5.) <= 1e-8 and metadata["insertion_shutdown_until_s"] == ss.time_s
        and orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= 220000.
        and orbit["apogee_altitude_m"] <= 600000. and ss.propellant_kg >= 75000.
        and ss.propellant_kg <= cs.propellant_kg+1e-6 and env.norm(ss.omega_body_rad_s) <= 5.
        and _same(settled["vehicle"],asdict(crafts[26])) and _same(settled["orbit"],orbit),
        "restored_actual_settled_insertion_gate_failed")
    schedule = {"final_gate_time_s":cs.time_s,"first_release_time_s":cs.time_s+45.,"return_request_time_s":cs.time_s+1800.}
    _require(_same(metadata["schedule"],schedule), "restored_schedule_must_use_actual_cutoff_not_event_or_fixed_clock")
    inventory = record["inventory"]
    _require(type(inventory) is dict and set(inventory) == {"schema","total_count","released_count","retained_count",
        "generation","sequencer_state","pending_action","observed_time_s","release_receipts"}
        and inventory["schema"] == INVENTORY_SCHEMA and type(inventory["total_count"]) is int
        and inventory["total_count"] == 26 and type(inventory["release_receipts"]) is list,
        "restored_invalid_inventory_schema")
    receipts = inventory["release_receipts"]
    retained = 26-len(receipts)
    _require(retained in (0,13,26) and all(type(inventory[key]) is int for key in ("released_count","retained_count","generation"))
        and inventory["released_count"] == inventory["generation"] == len(receipts)
        and inventory["retained_count"] == retained and inventory["pending_action"] is None
        and (inventory["sequencer_state"] in ("skipped","inhibited")
             or retained == 0 and inventory["sequencer_state"] == "running"),
        "restored_inventory_not_closed_or_receipt_derived")
    current = _state(record["final_state"],crafts[retained],outages)
    _require(record["phase"] == "orbital_coast" and ss.time_s <= current.time_s < schedule["return_request_time_s"]
        and _number(inventory["observed_time_s"]) and 0 <= current.time_s-inventory["observed_time_s"] <= 2.001
        and _same(record["final_vehicle"],asdict(crafts[retained])), "restored_origin_phase_clock_vehicle_or_freshness")
    previous_release = schedule["first_release_time_s"]-profile["payload"]["interval_s"]
    for index, receipt in enumerate(receipts):
        _release(receipt,profile,crafts[26-index],crafts[25-index],outages)
        now = receipt["time_s"]
        _require(now >= previous_release+profile["payload"]["interval_s"]-1e-8 and now <= current.time_s,
            "restored_release_schedule_or_order")
        previous_release = now
    history = record["state_history"]
    _require(type(history) is list and 1 <= len(history) <= 100000 and _same(history[0],{"state":settled["state"],"retained_count":26})
        and _same(history[-1],{"state":record["final_state"],"retained_count":retained}),
        "restored_actual_history_endpoints_mismatch")
    prior, count, seen = None, 26, 0
    for row in history:
        _require(type(row) is dict and set(row) == {"state","retained_count"}
            and type(row["retained_count"]) is int and row["retained_count"] in crafts, "restored_invalid_history_row")
        state = _state(row["state"],crafts[row["retained_count"]],outages)
        _require(ss.time_s <= state.time_s <= current.time_s and (prior is None or state.time_s >= prior.time_s
            and state.propellant_kg <= prior.propellant_kg+1e-6), "restored_history_clock_or_added_fuel")
        if row["retained_count"] != count:
            _require(prior is not None and row["retained_count"] == count-1 and seen < len(receipts)
                and _same(asdict(prior),receipts[seen]["parent_before_state"])
                and _same(row["state"],receipts[seen]["parent_after_state"]), "restored_history_release_chain_mismatch")
            count, seen = row["retained_count"], seen+1
        elif prior is not None and state.time_s == prior.time_s:
            _require(_same(asdict(prior),row["state"]), "restored_history_unexplained_same_time_reset")
        prior = state
    _require(seen == len(receipts), "restored_missing_actual_release_witness")
    current_orbit = _orbit(record["final_state"])
    _require(current_orbit["status"] == "bound" and _number(current_orbit["period_s"])
        and current_orbit["period_s"] > 0., "restored_origin_is_not_bound_orbital_coast")
    return {"schema":ORIGIN_SCHEMA,"kind":"restored_actual_state_evidence_only",
        "state":record["final_state"],"vehicle":record["final_vehicle"],"phase":"orbital_coast",
        "retained_count":retained,"released_count":len(receipts),"inventory_generation":len(receipts),
        "release_generation":len(receipts),
        "inventory_generation_semantics":"physical release receipt count only; excludes AI command revisions and legacy DeploymentTracker command generation",
        "command_revision":None,
        "inventory_receipt_chain_sha256":sha256(_encode(receipts)).hexdigest(),"schedule":schedule,
        "settled_insertion_time_s":ss.time_s,"orbit":current_orbit,
        "ascent_settings":settings,"booster_control_settings":record["booster_control_settings"],
        "source_sha256":source,"profile_sha256":PROFILE_SHA256,
        "source_provenance_is_not_authenticated_host_receipt":True,
        "history_scope":"provided actual coast/release witnesses; no unsampled dynamics proof",
        "old_certificate_admitted":False,"mission_authority_granted":False,"dispatch_authorized":False,
        "physical_execution_authorized":False,"qualification_refreshed":False}


@dataclass(frozen=True)
class ActualRestoredOrigin:
    source_record_sha256: str
    raw_record_bytes: InitVar[bytes]
    profile_raw_bytes: InitVar[bytes]
    expected_source_sha256: InitVar[dict]
    _origin_json: bytes = field(init=False,repr=False)
    _source_record_bytes: bytes = field(init=False,repr=False,compare=False)

    def __post_init__(self,raw_record_bytes,profile_raw_bytes,expected_source_sha256):
        _require(_hash(self.source_record_sha256) and type(raw_record_bytes) is bytes
            and sha256(raw_record_bytes).hexdigest() == self.source_record_sha256, "restored_source_record_sha256_mismatch")
        _require(type(profile_raw_bytes) is bytes and sha256(profile_raw_bytes).hexdigest() == PROFILE_SHA256,
            "restored_fixed_profile_raw_sha256_mismatch")
        _manifest(expected_source_sha256)
        try:
            origin = _validate(_read(raw_record_bytes),_read(profile_raw_bytes),_read(_encode(expected_source_sha256)))
        except (KeyError,TypeError,AttributeError,OverflowError,ZeroDivisionError,IndexError) as error:
            raise ValueError("restored_incomplete_or_invalid_actual_evidence") from error
        origin["source_record_sha256"] = self.source_record_sha256
        object.__setattr__(self,"_origin_json",_encode(origin))
        object.__setattr__(self,"_source_record_bytes",raw_record_bytes)

    def record(self):
        return _read(self._origin_json)

    def source_record_bytes(self):
        """Original immutable handoff bytes, not a prediction or execution grant."""
        return self._source_record_bytes

    @property
    def time_s(self):
        return self.record()["state"]["time_s"]

    def validate_profile(self,profile_raw_bytes):
        _require(type(profile_raw_bytes) is bytes and sha256(profile_raw_bytes).hexdigest() == PROFILE_SHA256,
            "restored_fixed_profile_raw_sha256_mismatch")


@dataclass(frozen=True)
class RestoredCandidateSchedule:
    """Current-state opportunities, optionally bound to an earlier typed epoch.

    An anchored schedule still reports its current osculating period, but that
    diagnostic cannot move the original nominal or next-orbit reservation.
    """
    origin: ActualRestoredOrigin
    maximum_simulation_time_s: float = MAXIMUM_SIMULATION_TIME_S
    anchor: RestoredCandidateSchedule | None = field(default=None, repr=False, compare=False)
    _schedule_json: bytes = field(init=False,repr=False)

    def __post_init__(self):
        _require(type(self.origin) is ActualRestoredOrigin and _number(self.maximum_simulation_time_s)
            and self.origin.time_s < self.maximum_simulation_time_s <= MAXIMUM_SIMULATION_TIME_S,
            "restored_candidate_schedule_requires_typed_origin_and_bounded_horizon")
        row = self.origin.record()
        nominal, period = row["schedule"]["return_request_time_s"],row["orbit"]["period_s"]
        next_orbit = nominal+period
        provenance = None
        if self.anchor is not None:
            _require(type(self.anchor) is RestoredCandidateSchedule, "restored_opportunity_anchor_requires_typed_schedule")
            prior, prior_origin = self.anchor.record(), self.anchor.origin.record()
            _require(self.maximum_simulation_time_s == prior["maximum_simulation_time_s"]
                and self.origin.time_s >= self.anchor.origin.time_s
                and all(_same(row[key],prior_origin[key]) for key in ("source_sha256","profile_sha256",
                    "ascent_settings","booster_control_settings","inventory_receipt_chain_sha256",
                    "inventory_generation","release_generation","released_count","retained_count","schedule")),
                "restored_opportunity_anchor_source_inventory_clock_or_horizon_changed")
            nominal, next_orbit = prior["nominal_s"], prior["next_orbit_s"]
            provenance = self.anchor.opportunity_anchor_record()
        _require(self.origin.time_s < nominal < next_orbit < self.maximum_simulation_time_s,
            "restored_next_orbit_outside_explicit_scope")
        result = {"schema":"missionos.starship_restored_candidate_schedule.v1",
            "origin_sha256":sha256(self.origin._origin_json).hexdigest(),"origin_time_s":self.origin.time_s,
            "actual_cutoff_time_s":row["schedule"]["final_gate_time_s"],"nominal_s":nominal,
            "next_orbit_s":next_orbit,"actual_current_orbital_period_s":period,
            "maximum_simulation_time_s":float(self.maximum_simulation_time_s),
            "old_certificate_admitted":False,"mission_authority_granted":False,"dispatch_authorized":False}
        if provenance is not None:
            result["opportunity_anchor"] = provenance
        object.__setattr__(self,"_schedule_json",_encode(result))

    def record(self):
        return _read(self._schedule_json)

    def opportunity_anchor_record(self):
        """Original immutable opportunity epoch; no AI-provided retiming."""
        row = self.record()
        if "opportunity_anchor" in row:
            return row["opportunity_anchor"]
        return {"schema":"missionos.starship_restored_opportunity_anchor.v1",
            "schedule_sha256":sha256(self._schedule_json).hexdigest(),"origin_sha256":row["origin_sha256"],
            "source_record_sha256":self.origin.source_record_sha256,"origin_time_s":row["origin_time_s"],
            "actual_cutoff_time_s":row["actual_cutoff_time_s"],"nominal_s":row["nominal_s"],
            "next_orbit_s":row["next_orbit_s"],"opportunity_orbital_period_s":row["actual_current_orbital_period_s"],
            "maximum_simulation_time_s":row["maximum_simulation_time_s"]}

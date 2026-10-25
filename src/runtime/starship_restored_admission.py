"""Experimental admission of one copied-current deterministic return forecast.

No observation uncertainty envelope, independent physics, old39 qualification,
operator approval or dispatch grant is established here. Energy, temperature
and modeled propellant loss are synthetic accounting assumptions, not added
forces or physical subsystem simulation. All numerical choices are tool inputs,
never numbers reported by a model.

``candidate_sha256`` identifies the mutable numerical/context proof. Operator
approval adapters MUST bind their candidate identity to ``proposal_sha256``; a
fresh forecast/context proof may change without changing that fixed proposal.
This module never converts either hash into an approval or dispatch grant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import re

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_restored_contract as contract
from . import starship_sixdof_verifier as saved
from .starship_restored_return import RestoredReturnResult, RestoredReturnExecution
from .starship_retained_return import TRIMMED_POLICY_ID


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _raw(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def _hash(value):
    return sha256(_raw(value)).hexdigest()


def _id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}",value) is not None


@dataclass(frozen=True)
class RestoredAdmissionPolicy:
    """Fixed registered engineering assumptions; no caller qualification flag."""

    def record(self):
        return {"schema":"missionos.starship_restored_admission_policy.v1",
            "fresh_state_match":{"position_m":50.,"velocity_mps":1.,"attitude_deg":1.,
                "body_rate_rad_s":.001,"fuel_kg":1.,"maximum_sample_gap_s":2.01,
                "throttle":.001,"gimbal_rad":.0001,"flap_rad":.0001},
            "contact":{"maximum_speed_mps":5.,"maximum_tilt_deg":5.,"maximum_body_rate_rad_s":.02,
                "minimum_propellant_kg":28000.},"booking_guard_s":60.,
            "waiting":{"initial_energy_kwh":22.,"power_kw":6.,"minimum_energy_kwh":10.,
                "initial_temperature_k":295.,"warming_k_per_s":.001,"maximum_temperature_k":310.,
                "modeled_loss_kg_per_s":.02},
            "one_copied_current_deterministic_forecast_only":True,"uncertainty_envelope_verified":False,
            "old_certificate_admitted":False,"mission_authority_granted":False,"dispatch_authorized":False}


@dataclass(frozen=True)
class RestoredSitePolicy:
    site_id: str
    latitude_deg: float
    longitude_deg: float
    radius_m: float
    opens_s: float
    closes_s: float

    def __post_init__(self):
        _require(_id(self.site_id) and all(_number(getattr(self,key)) for key in (
            "latitude_deg","longitude_deg","radius_m","opens_s","closes_s"))
            and -90. <= self.latitude_deg <= 90. and -180. <= self.longitude_deg <= 180.
            and 0 < self.radius_m <= 500000. and 0 <= self.opens_s < self.closes_s <= 16000.,
            "restored_invalid_registered_synthetic_site")

    def record(self):
        return {"site_id":self.site_id,"latitude_deg":float(self.latitude_deg),"longitude_deg":float(self.longitude_deg),
            "radius_m":float(self.radius_m),"opens_s":float(self.opens_s),"closes_s":float(self.closes_s),
            "synthetic_operating_area_only":True,"real_ocean_clearance_verified":False}


@dataclass(frozen=True)
class RestoredOperationalNotice:
    generation: int
    issued_s: float
    expires_s: float
    available_site_ids: tuple[str,...]

    def __post_init__(self):
        _require(type(self.generation) is int and self.generation >= 0
            and _number(self.issued_s) and _number(self.expires_s) and 0 <= self.issued_s < self.expires_s <= 16000.
            and type(self.available_site_ids) is tuple and len(self.available_site_ids) <= 16
            and all(_id(value) for value in self.available_site_ids)
            and len(set(self.available_site_ids)) == len(self.available_site_ids),"restored_invalid_operational_notice")

    def record(self):
        return {"schema":"missionos.starship_restored_operational_notice.v1","generation":self.generation,
            "issued_s":float(self.issued_s),"expires_s":float(self.expires_s),
            "available_site_ids":sorted(self.available_site_ids),"synthetic_operations_only":True}


@dataclass(frozen=True)
class RestoredResourceState:
    generation: int
    observed_s: float
    energy_kwh: float = 22.
    temperature_k: float = 295.
    modeled_propellant_loss_kg: float = 0.

    def __post_init__(self):
        _require(type(self.generation) is int and self.generation >= 0
            and all(_number(getattr(self,key)) for key in ("observed_s","energy_kwh","temperature_k","modeled_propellant_loss_kg"))
            and 0 <= self.observed_s <= 16000. and 0 <= self.energy_kwh <= 22.
            and 0 < self.temperature_k <= 1000. and 0 <= self.modeled_propellant_loss_kg <= 100000.,
            "restored_invalid_synthetic_resource_observation")

    def record(self):
        return {"schema":"missionos.starship_restored_resource_state.v1","generation":self.generation,
            "observed_s":float(self.observed_s),"energy_kwh":float(self.energy_kwh),"temperature_k":float(self.temperature_k),
            "modeled_propellant_loss_kg":float(self.modeled_propellant_loss_kg),
            "synthetic_accounting_only":True,"physical_state_modified":False}


@dataclass(frozen=True,init=False)
class RestoredCurrentObservation:
    """Read one actual driver snapshot; never an initialization or permission."""
    _json: bytes = field(repr=False)

    def __init__(self, closed_origin, execution):
        _require(type(closed_origin) is contract.ActualRestoredOrigin and type(execution) is RestoredReturnExecution,
            "restored_observation_requires_closed_origin_and_actual_execution")
        original,snapshot = closed_origin.record(),execution.snapshot()
        _require(type(snapshot) is dict and snapshot.get("completed") is False
            and snapshot.get("phase") == "orbital_coast" and snapshot.get("return_controller") is None
            and contract._same(snapshot.get("vehicle"),original["vehicle"]),"restored_observation_not_current_actual_coast")
        craft = dyn.vehicle_from_dict(original["vehicle"])
        outages = {row["engine_name"] for row in original["ascent_settings"]["ascent"]["shutdowns"]
            if row["engine_name"].startswith("ship_")}
        state = contract._state(snapshot["state"],craft,outages)
        binding = snapshot["binding"]
        anchor = contract.RestoredCandidateSchedule(closed_origin)
        _require(original["state"]["time_s"] <= state.time_s <= binding["maximum_simulation_time_s"]
            and state.propellant_kg <= original["state"]["propellant_kg"]+1e-6
            and all(binding.get(key) == value for key,value in {
                "source_sha256":_hash(original["source_sha256"]),"profile_sha256":original["profile_sha256"],
                "inventory_receipt_chain_sha256":original["inventory_receipt_chain_sha256"],
                "inventory_generation":original["inventory_generation"],"retained_count":original["retained_count"],
                "released_count":original["released_count"],"controller_policy":TRIMMED_POLICY_ID,
                "maximum_simulation_time_s":16000.}.items())
            and binding.get("candidate_id") in ("nominal","next_orbit")
            and binding.get("return_time_s") == anchor.record()[binding["candidate_id"]+"_s"]
            and all(type(binding.get(key)) is int for key in ("inventory_generation","retained_count","released_count"))
            and all(binding.get(key) is False for key in ("old_certificate_admitted","registered_return",
                "qualification_refreshed","mission_authority_granted","dispatch_authorized")),
            "restored_observation_source_inventory_reservation_or_actual_state_mismatch")
        object.__setattr__(self,"_json",_raw({"schema":"missionos.starship_restored_current_observation.v1",
            "closed_origin_sha256":_hash(original),"source_sha256":original["source_sha256"],
            "profile_sha256":original["profile_sha256"],"inventory_receipt_chain_sha256":original["inventory_receipt_chain_sha256"],
            "release_generation":original["release_generation"],"retained_count":original["retained_count"],
            "state":snapshot["state"],"vehicle":snapshot["vehicle"],"phase":"orbital_coast","binding":binding,
            "opportunity_anchor":anchor.opportunity_anchor_record(),"actual_state_assigned":False,
            "provenance_is_not_authenticated_worker_receipt":True,"mission_authority_granted":False}))

    def record(self):
        return json.loads(self._json)


def _contact(prediction,origin,profile):
    """Independent saved geometry and current-load/actuator checks only."""
    from .starship_state_mission_metrics import _settings
    from .starship_restored_verifier import _observations
    before = origin.record()
    _require(prediction.get("schema") == "missionos.starship_restored_return_prediction.v1"
        and prediction.get("prediction_only") is True and prediction.get("actual_state_initialized_from_prediction") is False
        and contract._same(prediction.get("initial_state"),before["state"])
        and contract._same(prediction.get("vehicle"),before["vehicle"])
        and contract._same(prediction.get("final_vehicle"),before["vehicle"])
        and type(prediction.get("retained_count")) is int and prediction["retained_count"] == before["retained_count"]
        and prediction.get("payload_releases_after_origin") == 0 and type(prediction.get("payload_releases_after_origin")) is int
        and prediction.get("reservation_changes") == [] and type(prediction.get("model_calls")) is int and prediction["model_calls"] == 0
        and all(prediction.get(key) is False for key in ("old_certificate_admitted","qualification_refreshed",
            "mission_authority_granted","dispatch_authorized","physical_execution","source_provenance_authenticated","independent_physical_model")),
        "restored_prediction_source_state_inventory_or_scope_mismatch")
    first,last = saved._samples(prediction.get("samples"),"$.prediction.samples",[0],"ship",wind_profile=profile)
    _require(saved._same_state(first,before["state"]) and saved._same_state(last,prediction.get("final_state")),
        "restored_prediction_sample_endpoints_mismatch")
    craft = dyn.vehicle_from_dict(before["vehicle"])
    _observations(prediction["samples"],lambda _:craft,_settings(before["ascent_settings"],profile))
    outcome = prediction["outcome"]
    termination = outcome.get("termination")
    _require(termination in ("surface_impact","low_speed_surface_contact","angular_rate_envelope_exceeded","time_limit","control_tick_limit")
        and outcome.get("phase") == last["phase"] and type(outcome.get("integration_steps")) is int
        and type(outcome.get("control_ticks")) is int and 0 <= outcome["integration_steps"] <= outcome["control_ticks"] <= 200000
        and first["time_s"] <= last["time_s"] <= 16000.,"restored_prediction_invalid_numerical_record")
    events = saved._events(prediction.get("events"),"$.prediction.events",first["time_s"],last["time_s"])
    _require(len(events.get(termination,[])) == 1 and events[termination][0]["time_s"] == last["time_s"],
        "restored_prediction_terminal_event_mismatch")
    phases = ("orbital_coast","deorbit_slew","deorbit_burn","ballistic_return","landing_burn")
    _require(first["phase"] == "orbital_coast" and all(sample["phase"] in phases for sample in prediction["samples"])
        and all(phases.index(a["phase"]) <= phases.index(b["phase"]) for a,b in zip(prediction["samples"],prediction["samples"][1:])),
        "restored_prediction_phase_order_mismatch")
    requests = events.get("return_requested",[])
    _require("return_start_state" in prediction and len(requests) <= 1,"restored_prediction_missing_precontroller_witness")
    witness = prediction["return_start_state"]
    if requests:
        outages = {row["engine_name"] for row in before["ascent_settings"]["ascent"]["shutdowns"]
            if row["engine_name"].startswith("ship_")}
        started = contract._state(witness,craft,outages)
        _require(started.time_s == requests[0]["time_s"] and 0 <= started.time_s-prediction["binding"]["return_time_s"]
            <= profile["integration"]["coast_dt_s"]+1e-7
            and saved._same_state(witness,prediction.get("return_controller",{}).get("record",{}).get("activation",{}).get("state",{}))
            and any(sample["time_s"] == started.time_s and saved._same_state(sample,witness) for sample in prediction["samples"]),
            "restored_prediction_precontroller_witness_time_or_state_mismatch")
    else:
        _require(witness is None,"restored_prediction_precontroller_witness_without_request")
    contact = termination in ("surface_impact","low_speed_surface_contact")
    _require(last.get("contact") is contact,"restored_prediction_contact_flag_mismatch")
    if not contact:
        _require(outcome.get("contact_receipt") is None,"restored_prediction_noncontact_has_receipt")
        return {"contact_observed":False,"capability_met":False,"gates":{}}
    controller = prediction.get("return_controller",{})
    _require(len(requests) == 1 and 0 <= requests[0]["time_s"]-prediction["binding"]["return_time_s"]
        <= profile["integration"]["coast_dt_s"]+1e-7 and controller.get("policy") == TRIMMED_POLICY_ID
        and controller.get("allocation_enabled") is True and controller.get("registered") is False
        and controller.get("released_count") == before["released_count"],"restored_prediction_return_policy_or_inventory_mismatch")
    saved._return_allocation_scope({"samples":prediction["samples"],"retained_return":controller["record"]},
        {"bounded_ship_flaps":True,"application":"wind_trim_state_return_v2"},"$.prediction",profile)
    receipt = outcome["contact_receipt"]
    speed = saved._contact(receipt,last,"$.prediction.contact",profile["geometry"]["ship_length_m"],profile["geometry"]["radius_m"])
    _require(events[termination][0].get("contact") == receipt
        and (speed <= 5.) is (termination == "low_speed_surface_contact"),"restored_prediction_contact_event_or_classification_mismatch")
    axis = dyn.rotate(last["q_body_to_eci"],(0.,0.,1.))
    tilt = math.degrees(math.acos(max(-1.,min(1.,env.dot(axis,receipt["surface_normal_eci"])))))
    rate = env.norm(last["omega_body_rad_s"])
    gates = {"nonoverlapping_descending_contact":receipt["initial_overlap"] is False and receipt["surface_normal_speed_mps"] <= 0.,
        "speed":speed <= 5.,"tilt":tilt <= 5.,"body_rate":rate <= .02,"reserve":last["propellant_kg"] >= 28000.}
    ecef = env.eci_to_ecef(receipt["point_eci_m"],receipt["event_time_s"])
    latitude,longitude,_ = env.ecef_to_geodetic(ecef)
    return {"contact_observed":True,"capability_met":all(gates.values()),"gates":gates,
        "event_time_s":receipt["event_time_s"],"point_speed_mps":speed,"tilt_deg":tilt,
        "body_rate_rad_s":rate,"propellant_kg":last["propellant_kg"],
        "latitude_deg":math.degrees(latitude),"longitude_deg":math.degrees(longitude)}


@dataclass(frozen=True,init=False)
class RestoredCandidate:
    _json: bytes = field(repr=False)
    _prediction_json: bytes = field(repr=False)
    _origin_json: bytes = field(repr=False)

    def __init__(self,prediction,forecast_origin,opportunity_anchor,profile_raw_bytes,expected_source_sha256,
                 policy,sites,notice,resources):
        _require(type(prediction) is RestoredReturnResult and type(forecast_origin) is contract.ActualRestoredOrigin
            and type(opportunity_anchor) is contract.RestoredCandidateSchedule and type(policy) is RestoredAdmissionPolicy
            and type(sites) is tuple and 1 <= len(sites) <= 16 and all(type(site) is RestoredSitePolicy for site in sites)
            and len({site.site_id for site in sites}) == len(sites)
            and type(notice) is RestoredOperationalNotice and type(resources) is RestoredResourceState,
            "restored_candidate_requires_exact_typed_tool_inputs")
        forecast_origin.validate_profile(profile_raw_bytes)
        origin,raw,profile = forecast_origin.record(),prediction.record(),json.loads(profile_raw_bytes)
        _require(contract._same(expected_source_sha256,origin["source_sha256"]),"restored_candidate_host_source_mismatch")
        _require("src/runtime/starship_restored_admission.py" in expected_source_sha256,"restored_admission_source_pin_missing")
        anchored = contract.RestoredCandidateSchedule(forecast_origin,16000.,anchor=opportunity_anchor)
        schedules = [anchored.record()]
        if contract._same(forecast_origin.record(),opportunity_anchor.origin.record()):
            schedules.append(opportunity_anchor.record())
        binding = raw["binding"]
        candidate = binding.get("candidate_id")
        _require(candidate in ("nominal","next_orbit") and type(binding.get("maximum_control_ticks")) is int
            and 1 <= binding["maximum_control_ticks"] <= 200000,"restored_candidate_unknown_or_unbounded_forecast")
        schedule = next((row for row in schedules if binding.get("schedule_sha256") == _hash(row)),None)
        _require(schedule is not None,"restored_candidate_forecast_retimed_frozen_opportunity")
        expected = {"schema":"missionos.starship_restored_return_request.v1","origin_sha256":_hash(origin),
            "source_record_sha256":forecast_origin.source_record_sha256,"source_sha256":_hash(expected_source_sha256),
            "profile_sha256":contract.PROFILE_SHA256,"inventory_receipt_chain_sha256":origin["inventory_receipt_chain_sha256"],
            "inventory_generation":origin["inventory_generation"],"retained_count":origin["retained_count"],"released_count":origin["released_count"],
            "schedule_sha256":_hash(schedule),"candidate_id":candidate,"return_time_s":schedule[candidate+"_s"],
            "origin_time_s":origin["state"]["time_s"],"maximum_simulation_time_s":16000.,
            "maximum_control_ticks":binding["maximum_control_ticks"],"return_start_clock_policy":"first_existing_control_tick_at_or_after_candidate_time",
            "maximum_start_clock_slip_s":profile["integration"]["coast_dt_s"],"controller_policy":TRIMMED_POLICY_ID,
            "bounded_ship_flaps":True,"registered_return":False,"old_certificate_admitted":False,"qualification_refreshed":False,
            "mission_authority_granted":False,"dispatch_authorized":False}
        if "opportunity_anchor" in schedule:
            expected["opportunity_anchor"] = schedule["opportunity_anchor"]
        _require(contract._same(binding,expected) and contract._same(raw.get("initial_binding"),expected),
            "restored_candidate_prediction_binding_mismatch")
        metrics = _contact(raw,forecast_origin,profile)
        site_rows = sorted((site.record() for site in sites),key=lambda row:row["site_id"])
        proposal = {"schema":"missionos.starship_restored_return_proposal.v1","candidate_id":candidate,
            "return_time_s":expected["return_time_s"],"opportunity_anchor":opportunity_anchor.opportunity_anchor_record(),
            "source_sha256":_hash(expected_source_sha256),"profile_sha256":contract.PROFILE_SHA256,
            "inventory_receipt_chain_sha256":origin["inventory_receipt_chain_sha256"],"release_generation":origin["release_generation"],
            "retained_count":origin["retained_count"],"controller_policy":TRIMMED_POLICY_ID,
            "admission_policy_sha256":_hash(policy.record()),"site_policy_sha256":_hash(site_rows)}
        proof = {"proposal_sha256":_hash(proposal),"prediction_record_sha256":prediction.sha256,"forecast_origin_sha256":_hash(origin),
            "notice_sha256":_hash(notice.record()),"resource_sha256":_hash(resources.record()),
            "notice_generation":notice.generation,"resource_generation":resources.generation}
        row = {"schema":"missionos.starship_restored_admission_candidate.v1",**proof,
            "candidate_sha256":_hash(proof),"forecast_proof_sha256":_hash(proof),"proposal":proposal,
            "inventory_generation":origin["inventory_generation"],"retained_count":origin["retained_count"],
            "policy":policy.record(),"sites":site_rows,"notice":notice.record(),"resources":resources.record(),
            "numerical_contact":metrics,"prediction_only":True,"actual_state_assigned":False,
            "uncertainty_envelope_verified":False,"old_certificate_admitted":False,"mission_authority_granted":False,"dispatch_authorized":False}
        object.__setattr__(self,"_json",_raw(row))
        object.__setattr__(self,"_prediction_json",prediction._record_json)
        object.__setattr__(self,"_origin_json",_raw(origin))

    def record(self):
        return json.loads(self._json)


def _distance(a,b):
    return math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))


def _fresh(prediction,actual,limits):
    """Adjacent continuous coast witnesses only; no propagation/extrapolation."""
    now,samples = actual["time_s"],prediction["samples"]
    witness = prediction.get("return_start_state")
    exact_witness = witness is not None and abs(witness["time_s"]-now) <= 1e-8
    exact = [witness] if exact_witness else [sample for sample in samples
        if sample["phase"] == "orbital_coast" and abs(sample["time_s"]-now) <= 1e-8]
    bracket = (exact[-1],exact[-1]) if exact else next(((left,right) for left,right in zip(samples,samples[1:])
        if left["phase"] == right["phase"] == "orbital_coast" and left["time_s"] < now < right["time_s"]),None)
    if bracket is None:
        return ["actual_state_outside_continuous_predicted_coast"],None
    a,b = bracket
    gap = b["time_s"]-a["time_s"]
    if gap > limits["maximum_sample_gap_s"]:
        return ["prediction_coast_sample_gap"],None
    if gap and any(a["time_s"] < event["time_s"] <= b["time_s"] for event in prediction["events"]):
        return ["prediction_coast_event_straddle"],None
    fraction = 0. if not gap else (now-a["time_s"])/gap
    reasons,errors = [],{}
    for name,limit in (("r_eci_m","position_m"),("v_eci_mps","velocity_mps"),("omega_body_rad_s","body_rate_rad_s")):
        estimate = [x+fraction*(y-x) for x,y in zip(a[name],b[name])]
        errors[limit] = _distance(estimate,actual[name])
        if errors[limit] > limits[limit]:
            reasons.append("fresh_"+limit)
    qa,qb = a["q_body_to_eci"],b["q_body_to_eci"]
    if sum(x*y for x,y in zip(qa,qb)) < 0:
        qb = [-x for x in qb]
    q = [x+fraction*(y-x) for x,y in zip(qa,qb)]
    norm = math.sqrt(sum(x*x for x in q))
    actual_norm = math.sqrt(sum(x*x for x in actual["q_body_to_eci"]))
    dot = abs(sum(x*y for x,y in zip(q,actual["q_body_to_eci"]))/norm/actual_norm)
    errors["attitude_deg"] = math.degrees(2*math.acos(min(1.,dot)))
    errors["fuel_kg"] = abs(a["propellant_kg"]+fraction*(b["propellant_kg"]-a["propellant_kg"])-actual["propellant_kg"])
    for key in ("attitude_deg","fuel_kg"):
        if errors[key] > limits[key]:
            reasons.append("fresh_"+key)
    if ([row["available"] for row in actual["engine_states"]] != [row["available"] for row in a["engine_states"]]
            or [row["available"] for row in a["engine_states"]] != [row["available"] for row in b["engine_states"]]):
        reasons.append("fresh_actual_engine_availability")
    for key,name in (("throttle","throttle"),("gimbal_x_rad","gimbal_rad"),("gimbal_y_rad","gimbal_rad")):
        difference = max((abs(left[key]+fraction*(right[key]-left[key])-observed[key])
            for left,right,observed in zip(a["engine_states"],b["engine_states"],actual["engine_states"])),default=0.)
        errors[name] = max(errors.get(name,0.),difference)
    errors["flap_rad"] = max((abs(left+fraction*(right-left)-observed)
        for left,right,observed in zip(a["flap_angles_rad"],b["flap_angles_rad"],actual["flap_angles_rad"])),default=0.)
    reasons.extend("fresh_"+key for key in ("throttle","gimbal_rad","flap_rad") if errors[key] > limits[key])
    return reasons,{"left_time_s":a["time_s"],"right_time_s":b["time_s"],"fraction":fraction,"errors":errors,
        "method":"exact_precontroller_return_start_state" if exact_witness else
            "continuous_coast_linear_state_and_hemisphere_normalized_quaternion_interpolation_not_error_bound"}


def _site_distance(metrics,site):
    p,q = map(math.radians,(metrics["latitude_deg"],site["latitude_deg"]))
    longitude = math.radians(metrics["longitude_deg"]-site["longitude_deg"])
    h = math.sin((p-q)/2.)**2+math.cos(p)*math.cos(q)*math.sin(longitude/2.)**2
    return env.EARTH_EQUATORIAL_RADIUS_M*2.*math.asin(math.sqrt(max(0.,min(1.,h))))


def admit_restored_candidate(candidate,observation,*,notice,resources,stage):
    """Recheck at selection AND first due dispatch tick; grants no authority.

    A new notice/resource snapshot requires a newly bound candidate proof. Its
    proposal identity stays fixed, so fresh numerical evidence never changes an
    operator's approved opportunity or smuggles in a new return time.
    """
    _require(type(candidate) is RestoredCandidate and type(observation) is RestoredCurrentObservation
        and type(notice) is RestoredOperationalNotice and type(resources) is RestoredResourceState
        and stage in ("selection","dispatch"),"restored_admission_requires_exact_typed_current_inputs")
    row,current = candidate.record(),observation.record()
    origin = json.loads(candidate._origin_json)
    prediction = json.loads(candidate._prediction_json)
    now = current["state"]["time_s"]
    proposal,policy = row["proposal"],row["policy"]
    when,anchor = proposal["return_time_s"],proposal["opportunity_anchor"]
    reasons = []
    if not (contract._same(current["source_sha256"],origin["source_sha256"])
            and current["profile_sha256"] == origin["profile_sha256"]
            and current["inventory_receipt_chain_sha256"] == origin["inventory_receipt_chain_sha256"]
            and current["release_generation"] == origin["release_generation"]
            and current["retained_count"] == origin["retained_count"]
            and contract._same(current["vehicle"],origin["vehicle"])
            and contract._same(current["opportunity_anchor"],anchor)):
        reasons.append("current_source_inventory_vehicle_or_frozen_opportunity_mismatch")
    if _hash(notice.record()) != row["notice_sha256"] or notice.generation != row["notice_generation"]:
        reasons.append("notice_generation_or_content_changed_rebind_saved_prediction")
    if _hash(resources.record()) != row["resource_sha256"] or resources.generation != row["resource_generation"]:
        reasons.append("resource_generation_or_content_changed_rebind_saved_prediction")
    if not notice.issued_s <= now <= notice.expires_s:
        reasons.append("notice_stale")
    if stage == "selection":
        if now > anchor["nominal_s"]-policy["booking_guard_s"]:
            reasons.append("original_nominal_booking_guard")
    elif not (0 <= now-when <= current["binding"]["maximum_start_clock_slip_s"]+1e-7
            and current["binding"]["candidate_id"] == proposal["candidate_id"] and current["binding"]["return_time_s"] == when):
        reasons.append("dispatch_not_first_due_reserved_tick")
    fresh_reasons,fresh = _fresh(prediction,current["state"],policy["fresh_state_match"])
    reasons.extend(fresh_reasons)
    metrics = row["numerical_contact"]
    if not metrics["capability_met"]:
        reasons.append("numerical_contact_capability_unmet")
    matched = []
    if metrics["contact_observed"]:
        matched = [site["site_id"] for site in row["sites"] if site["site_id"] in notice.available_site_ids
            and _site_distance(metrics,site) <= site["radius_m"]
            and site["opens_s"] <= metrics["event_time_s"] <= site["closes_s"]
            and metrics["event_time_s"] <= notice.expires_s]
    if not matched:
        reasons.append("synthetic_area_or_contact_availability_window")
    waiting = max(0.,when-resources.observed_s)
    account = {"prospective_orbital_wait_s":waiting,
        "energy_at_return_kwh":resources.energy_kwh-waiting*policy["waiting"]["power_kw"]/3600.,
        "temperature_at_return_k":resources.temperature_k+waiting*policy["waiting"]["warming_k_per_s"],
        "modeled_loss_budget_kg":resources.modeled_propellant_loss_kg+waiting*policy["waiting"]["modeled_loss_kg_per_s"],
        "accounting_assumptions_not_physical_state":True}
    # The existing dispatch gate separately bounds the first actual due tick.
    # Its current resource observation may include that allowed clock slip;
    # selection still cannot borrow an observation after the reserved epoch.
    if resources.observed_s > now or stage == "selection" and resources.observed_s > when:
        reasons.append("resource_observation_is_future")
    if account["energy_at_return_kwh"] < policy["waiting"]["minimum_energy_kwh"]:
        reasons.append("synthetic_energy_budget")
    if account["temperature_at_return_k"] > policy["waiting"]["maximum_temperature_k"]:
        reasons.append("synthetic_temperature_budget")
    account["reserve_after_modeled_loss_kg"] = (metrics.get("propellant_kg",0.)-account["modeled_loss_budget_kg"])
    if account["reserve_after_modeled_loss_kg"] < policy["contact"]["minimum_propellant_kg"]:
        reasons.append("synthetic_reserve_after_modeled_loss_budget")
    return {"schema":"missionos.starship_restored_candidate_admission.v1","accepted":not reasons,
        "reasons":sorted(set(reasons)),"stage":stage,"time_s":now,"proposal_sha256":row["proposal_sha256"],
        "candidate_sha256":row["candidate_sha256"],"forecast_proof_sha256":row["forecast_proof_sha256"],
        "prediction_record_sha256":row["prediction_record_sha256"],"observation_sha256":_hash(current),
        "notice_sha256":_hash(notice.record()),"resource_sha256":_hash(resources.record()),
        "matched_synthetic_site_ids":sorted(matched),"fresh_state_match":fresh,"resource_accounting":account,
        "numerical_contact_capability_met":metrics["capability_met"],"uncertainty_envelope_verified":False,
        "one_copied_current_deterministic_forecast_only":True,"old_certificate_admitted":False,
        "mission_authority_granted":False,"dispatch_authorized":False,"physical_state_modified":False}

"""Saved-only ground-prefix/actual-return consistency for a new explicit scope.

No old return certificate, producer, controller, integration or model is called.
The execution envelope is checked against externally supplied expected values;
its equality is not an authenticated worker signature or operator approval.
Sparse return samples do not independently prove unsampled plant dynamics.
"""
from hashlib import sha256
import math
from pathlib import Path

from . import starship_physics as env, starship_sixdof as dyn
from . import starship_restored_contract as contract
from . import starship_sixdof_verifier as saved
from . import starship_state_mission_metrics as state_checks
from . import starship_booster_terminal_metrics as booster_checks
from .starship_experimental_continuation import development_settings
from .starship_experimental_mission_metrics import (
    FORBIDDEN, STATE_FIELDS, digest, _require, _same, _vehicle_for_sample, _clock)
from .starship_retained_return import TRIMMED_POLICY_ID

SCHEMA = "missionos.starship_restored_mission.v1"
EXECUTION_SCHEMA = "missionos.starship_restored_mission_execution.v1"
INTEGRATED_SCHEMA = "missionos.starship_restored_mission.v2"
INTEGRATED_EXECUTION_SCHEMA = "missionos.starship_restored_mission_execution.v2"


def _expected_profile(profile):
    raw = (Path(__file__).resolve().parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    _require(sha256(raw).hexdigest() == contract.PROFILE_SHA256
        and contract._encode(contract._read(raw)) == contract._encode(profile), "Fixed raw profile or parsed profile differs")
    return raw


def _prefix_basics(record, profile):
    termination = record["outcome"]["termination"]
    if termination not in ("restored_deployment_handoff","restored_deployment_unclosed"):
        return state_checks._run_state_basics(record,profile)[:4]
    first,last = saved._samples(record.get("samples"),"$.prefix.samples",[0],"ship",wind_profile=profile)
    outcome = record["outcome"]
    _require(_same(first,record["initial_state"]) and _same(last,record["final_state"])
        and first["time_s"] == 0. and first["phase"] == "stack_ascent"
        and last["phase"] == outcome["phase"] == "orbital_coast"
        and last.get("contact") is False and outcome.get("contact_receipt") is None
        and outcome.get("six_dof_integrated") is True and outcome.get("attitude_prescribed") is False
        and outcome.get("starship_vehicle_validated") is False
        and type(outcome["integration_steps"]) is int and outcome["integration_steps"] > 0
        and saved._near(outcome["duration_s"],last["time_s"])
        and saved._near(outcome["final_altitude_m"],last["altitude_m"])
        and saved._near(outcome["final_ground_speed_mps"],last["ground_speed_mps"])
        and outcome["max_altitude_m"] >= max(row["altitude_m"] for row in record["samples"])-1e-5,
        "Restored prefix lost actual ground/state/clock or falsely claimed contact")
    events = saved._events(record.get("events"),"$.prefix.events",0.,last["time_s"])
    _require(len(events.get(termination,[])) == 1 and events[termination][0]["time_s"] == last["time_s"]
        and not events.get("return_requested"), "Prefix stop lacks exact event or dispatched a return")
    return first,last,outcome,events


def _observations(samples, craft_for, settings):
    prior = {}
    for sample in samples:
        craft = craft_for(sample)
        state = dyn.state_from_dict({key:sample[key] for key in STATE_FIELDS})
        command = dyn.command_from_dict(sample["command"]) if sample.get("command") is not None else dyn.Command6DOF(
            tuple(dyn.EngineCommand() for _ in craft.engines),state.flap_angles_rad)
        dyn._validate_pair(state,craft,command)
        kind = "booster" if sample["phase"] == "stack_ascent" else "ship"
        _require(state_checks._check_availability(sample,kind,settings),"Actual engine availability changed")
        _require(kind not in prior or state.propellant_kg <= prior[kind]+1e-6, "Actual coast/return gained propellant")
        prior[kind] = state.propellant_kg
        actual = dyn.observe(state,craft)
        for key in ("altitude_m","mass_kg","dynamic_pressure_pa","com_z_m","com_rate_body_mps"):
            expected = actual["com_body_m"][2] if key == "com_z_m" else actual[key]
            if isinstance(expected,(tuple,list)):
                _require(type(sample.get(key)) is list and len(sample[key]) == len(expected)
                    and all(saved._near(x,y,1e-5) for x,y in zip(sample[key],expected)), "Saved moving CoM observation differs")
            else:
                _require(saved._near(sample.get(key),expected,1e-5), "Saved observation differs from current actual craft")


def _decisions(record, handoff, profile, *, extra_metadata_validator=None):
    """Keep actual inventory/state/order checks for every decision.

    Legacy callers admit callback metadata only. A composed verifier may supply
    a checker for a separately verified, non-model supervisor stop; that checker
    cannot bypass the common action, inventory, numerical readiness or clock.
    """
    decisions = record["restored_prefix"].get("decisions")
    retained = handoff["inventory"]["retained_count"]
    expected = [(0,"withhold_deployment")] if retained == 26 else [
        (0,"continue_deployment"),(13,"withhold_deployment" if retained == 13 else "continue_deployment")]
    _require(type(decisions) is list and len(decisions) == len(expected), "Missing or repeated deployment decision")
    releases = handoff["inventory"]["release_receipts"]
    for decision,(count,action) in zip(decisions,expected):
        summary = decision["summary"]
        ordinary = (decision.get("origin") == "external_callback_not_model_or_approval_proof"
            and summary.get("sequencer_state") == "running" and summary.get("notice") == "")
        metadata_valid = ordinary or (extra_metadata_validator is not None
            and extra_metadata_validator(decision, count, action) is True)
        _require(decision.get("action") == action and metadata_valid
            and summary.get("released_count") == count and type(summary.get("released_count")) is int
            and summary.get("retained_count") == 26-count and type(summary.get("retained_count")) is int
            and type(summary.get("release_ready")) is bool
            and handoff["experimental_mission_ascent"]["schedule"]["first_release_time_s"] <= summary["time_s"]
                <= handoff["final_state"]["time_s"]
            and (not count or releases[count-1]["time_s"] < summary["time_s"])
            and (count == len(releases) or summary["time_s"] <= releases[count]["time_s"]),
            "Decision does not match actual closed inventory or release order")
        witnesses = [s for s in record["samples"] if s["time_s"] == summary["time_s"] and s["phase"] == "orbital_coast"]
        _require(bool(witnesses), "Decision has no actual state witness")
        current = next((s for s in witnesses if sum(r["time_s"] < s["time_s"] for r in releases) == count),None)
        _require(current is not None,"Decision witness does not match the actual release count")
        bound,rp = saved._orbit(current)
        ready = bool(bound and rp >= profile["guidance"]["target_perigee_m"]-1000.
            and current["dynamic_pressure_pa"] < 1 and env.norm(current["omega_body_rad_s"]) < profile["guidance"]["release_max_rate_rad_s"])
        _require(summary["release_ready"] is ready, "Decision-time release readiness differs")
        failure,pending = decision.get("callback_error_type"),decision.get("pending")
        _require(failure is None or type(failure) is str and action == "withhold_deployment", "Callback failure did not fail closed")
        if pending is None:
            _require(summary.get("decision_started_s") is None and summary.get("decision_deadline_s") is None,
                "Nonpending decision borrowed a pending deadline")
        else:
            _require(type(pending) is dict and set(pending) == {"released_count","started_s","deadline_s","poll_count"}
                and pending["released_count"] == count and type(pending["poll_count"]) is int and pending["poll_count"] > 0
                and pending["deadline_s"] == pending["started_s"]+35.
                and pending["started_s"] < summary["time_s"] <= pending["deadline_s"]+profile["integration"]["coast_dt_s"]+1e-8
                and summary["decision_started_s"] == pending["started_s"] and summary["decision_deadline_s"] == pending["deadline_s"]
                and any(s["time_s"] == pending["started_s"] for s in record["samples"]), "Pending decision clock or witness differs")
            if failure == "SimulationDecisionDeadline":
                _require(summary["time_s"] >= pending["deadline_s"]-1e-9, "Decision timeout fired early")


def _origin(record, profile_raw, source):
    evidence = record["restored_prefix"]
    _require(evidence.get("schema") == "missionos.starship_restored_prefix.v1" and evidence.get("closed") is True
        and evidence.get("state_reset") is False and evidence.get("old_certificate_admitted") is False
        and evidence.get("mission_authority_granted") is False and evidence.get("model_calls_inferred") is False
        and evidence.get("pending_decision") is None and evidence.get("decision_window_s") == 35.
        and evidence.get("origin_validation_issue") is None,
        "Restored closed-prefix evidence scope differs")
    handoff = evidence["handoff_record"]
    raw = contract._encode(handoff)
    _require(sha256(raw).hexdigest() == evidence["handoff_record_sha256"], "Closed actual handoff hash differs")
    origin = contract.ActualRestoredOrigin(evidence["handoff_record_sha256"],raw,profile_raw,source)
    current = origin.record()
    _require(contract._same(evidence.get("origin"),current) and handoff["experimental_mission_ascent"] == record["experimental_mission_ascent"]
        and handoff["final_state"] == record["final_state"] and handoff["final_vehicle"] == record["final_vehicle"]
        and evidence.get("release_generation") == current["release_generation"]
        and handoff["inventory"]["release_receipts"] == [satellite["receipt"] for satellite in record["satellites"]],
        "Actual prefix endpoint, metadata, receipt chain or origin differs")
    return origin


def _fixed_return_binding(origin, profile, candidate, horizon):
    before = origin.record()
    schedule = contract.RestoredCandidateSchedule(origin,horizon).record()
    binding = {"schema":"missionos.starship_restored_return_request.v1","origin_sha256":digest(before),
        "source_record_sha256":origin.source_record_sha256,"source_sha256":digest(before["source_sha256"]),
        "profile_sha256":contract.PROFILE_SHA256,"inventory_receipt_chain_sha256":before["inventory_receipt_chain_sha256"],
        "inventory_generation":before["inventory_generation"],"retained_count":before["retained_count"],
        "released_count":before["released_count"],"schedule_sha256":digest(schedule),"candidate_id":candidate,
        "return_time_s":schedule[candidate+"_s"],"origin_time_s":before["state"]["time_s"],
        "maximum_simulation_time_s":horizon,"maximum_control_ticks":200000,
        "return_start_clock_policy":"first_existing_control_tick_at_or_after_candidate_time",
        "maximum_start_clock_slip_s":profile["integration"]["coast_dt_s"],"controller_policy":TRIMMED_POLICY_ID,
        "bounded_ship_flaps":True,"registered_return":False,"old_certificate_admitted":False,
        "qualification_refreshed":False,"mission_authority_granted":False,"dispatch_authorized":False}
    return binding


def _return(record, origin, profile, settings, candidate, horizon):
    """Legacy fixtures admit exactly one fixed reservation and no governance stop."""
    binding = _fixed_return_binding(origin,profile,candidate,float(horizon))
    return _return_physical(record,origin,profile,settings,horizon,initial_binding=binding,
        final_binding=binding,reservation_changes=[])


def _return_physical(record, origin, profile, settings, horizon, *, initial_binding, final_binding,
                     reservation_changes, allow_governance_stop=False):
    """Shared saved physical checks after a scope-specific binding validator."""
    horizon = float(horizon)
    before, binding = origin.record(), final_binding
    _require(type(record) is dict and record.get("schema") == "missionos.starship_restored_return_continuation.v1"
        and record.get("prediction_only") is False and record.get("actual_state_initialized_from_prediction") is False
        and not any(key in record for key in FORBIDDEN)
        and all(record.get(key) is False for key in ("source_provenance_authenticated","old_certificate_admitted",
            "qualification_refreshed","mission_authority_granted","dispatch_authorized","physical_execution","independent_physical_model"))
        and type(record.get("model_calls")) is int and record["model_calls"] == 0
        and record.get("initial_state") == before["state"] and record.get("vehicle") == before["vehicle"]
        and record.get("final_vehicle") == before["vehicle"] and type(record.get("retained_count")) is int
        and record["retained_count"] == before["retained_count"]
        and type(record.get("payload_releases_after_origin")) is int and record["payload_releases_after_origin"] == 0
        and contract._same(record.get("reservation_changes"),reservation_changes),
        "Actual return borrowed a prediction, reset, release or actor scope")
    _require(contract._same(record.get("binding"),binding) and contract._same(record.get("initial_binding"),initial_binding),
        "Actual return reservation/source/schedule differs")
    first,last = saved._samples(record.get("samples"),"$.return.samples",[0],"ship",wind_profile=profile)
    outcome = record["outcome"]
    termination = outcome.get("termination")
    allowed = {"surface_impact","low_speed_surface_contact","angular_rate_envelope_exceeded","time_limit","control_tick_limit"}
    if allow_governance_stop:
        allowed.add("return_inhibited_by_governance")
    _require(termination in allowed and _same(first,before["state"]) and _same(last,record["final_state"])
        and before["state"]["time_s"] <= last["time_s"] <= horizon and outcome["phase"] == last["phase"]
        and type(outcome["integration_steps"]) is int and type(outcome["control_ticks"]) is int
        and 0 <= outcome["integration_steps"] <= outcome["control_ticks"] <= binding["maximum_control_ticks"],
        "Actual return state, steps, termination or finite horizon differs")
    events = saved._events(record.get("events"),"$.return.events",first["time_s"],last["time_s"])
    _require(len(events.get(termination,[])) == 1 and events[termination][0]["time_s"] == last["time_s"],
        "Actual return lacks its exact terminal event")
    if termination == "time_limit":
        _require(last["time_s"] == horizon, "Return time limit stopped early")
    if termination == "control_tick_limit":
        _require(outcome["control_ticks"] == binding["maximum_control_ticks"], "Return tick budget stopped early")
    if termination == "angular_rate_envelope_exceeded":
        _require(env.norm(last["omega_body_rad_s"]) > 5., "Return rate guard lacks excessive actual rate")
    craft = dyn.vehicle_from_dict(before["vehicle"])
    _observations(record["samples"],lambda _:craft,settings)
    _require(last["propellant_kg"] <= before["state"]["propellant_kg"]+1e-6, "Return added fuel")
    returns = events.get("return_requested",[])
    _require(len(returns) <= 1, "Repeated actual return request")
    if returns:
        _require(0 <= returns[0]["time_s"]-binding["return_time_s"] <= binding["maximum_start_clock_slip_s"]+1e-7,
            "Actual return request retimed the registered candidate")
        controller = record["return_controller"]
        _require(controller.get("schema") == "missionos.ship_return_controller.v1" and controller.get("policy") == TRIMMED_POLICY_ID
            and controller.get("return_time_s") == binding["return_time_s"] and controller.get("released_count") == before["released_count"]
            and controller.get("allocation_enabled") is True and controller.get("registered") is False
            and controller.get("terminal_engine_out") is False and controller.get("phase") == outcome["phase"],
            "Actual return controller history changed retained inventory, policy or scope")
        retained = controller["record"]
        activation = retained.get("activation",{})
        _require(retained.get("policy_id") == TRIMMED_POLICY_ID and retained.get("landing_verified") is False
            and retained.get("starship_vehicle_validated") is False
            and activation.get("time_s") == returns[0]["time_s"]
            and activation.get("payload_retained_count") == before["retained_count"]
            and activation.get("payload_retained_mass_kg") == before["retained_count"]*profile["payload"]["mass_each_kg"]
            and any(_same(sample,activation.get("state",{})) for sample in record["samples"]), "Return activation lost actual retained mass or witness")
        saved._return_allocation_scope({"samples":record["samples"],"retained_return":retained},development_settings(),"$.return",profile)
    else:
        inhibited = allow_governance_stop and termination == "return_inhibited_by_governance"
        _require(record.get("return_controller") is None and (last["time_s"] < binding["return_time_s"]
            or inhibited and last["phase"] == "orbital_coast"
            and 0 <= last["time_s"]-binding["return_time_s"] <= binding["maximum_start_clock_slip_s"]+1e-7),
            "Due return lacks actual controller request")
        if inhibited:
            _require(record.get("return_start_state") is None
                and outcome.get("contact_receipt") is None
                and all(row["phase"] == "orbital_coast" for row in record["samples"]),
                "Governance-inhibited return activated a controller or claimed contact")
    receipt = outcome.get("contact_receipt")
    contact = termination in ("surface_impact","low_speed_surface_contact")
    _require(last.get("contact") is contact, "Return contact flag differs from termination")
    metrics = {"contact_observed":False,"contact_envelope_met":False,"gates":{}}
    if contact:
        speed = saved._contact(receipt,last,"$.return.contact",profile["geometry"]["ship_length_m"],profile["geometry"]["radius_m"])
        axis = dyn.rotate(last["q_body_to_eci"],(0.,0.,1.))
        tilt = math.degrees(math.acos(max(-1.,min(1.,env.dot(axis,receipt["surface_normal_eci"])))))
        rate = env.norm(last["omega_body_rad_s"])
        gates = {"nonoverlapping_descending_contact":receipt["initial_overlap"] is False and receipt["surface_normal_speed_mps"] <= 0,
            "contact_speed":speed <= 5.,"tilt":tilt <= 5.,"body_rate":rate <= .02,
            "reserve":last["propellant_kg"] >= profile["ship"]["return_reserve_kg"]}
        metrics = {"contact_observed":True,"point_speed_mps":speed,"point_normal_speed_mps":receipt["surface_normal_speed_mps"],
            "tilt_deg":tilt,"body_rate_rad_s":rate,"propellant_kg":last["propellant_kg"],
            "point_ecef_m":list(env.eci_to_ecef(receipt["point_eci_m"],receipt["event_time_s"])),
            "event_time_s":receipt["event_time_s"],"gates":gates,"contact_envelope_met":all(gates.values())}
        _require(bool(returns) and events[termination][0].get("contact") == receipt
            and (speed <= 5.) is (termination == "low_speed_surface_contact"), "Return contact event or speed classification differs")
    else:
        _require(receipt is None,"Noncontact return fabricated contact receipt")
    _require(contract._same(outcome.get("contact_metrics"),metrics), "Return contact metric or gate claim differs from independent geometry")
    return metrics


def _integration_receipt(record, policy):
    """Saved arithmetic/provenance only; no integration hook is installed here."""
    from .starship_restored_integration import RestoredIntegrationPolicy
    _require(type(policy) is RestoredIntegrationPolicy, "Exact typed restored integration policy required")
    receipt = record.get("numerical_integration", {})
    if policy.fuel_event_mode == "outer_bracket_v2":
        from .starship_restored_integration import validate_execution_receipt
        validate_execution_receipt(receipt, policy)
        return
    trace = receipt.get("trace", {}) if type(receipt) is dict else {}
    expected = {"schema":"missionos.starship_restored_integration_execution.v1", "policy":policy.record(),
        "status":"completed", "failure_class":None, "trace":trace, "process_isolation_required":True,
        "internal_rk4_only":True, "physics_limits_changed":False, "control_policy_changed":False,
        "experimental":True, "qualification_refreshed":False, "mission_authority_granted":False,
        "execution_permission_granted":False, "hosted_model_calls":0, "nested_sixdof_forecasts":0}
    _require(type(trace) is dict and set(trace) == {"rk4_calls","rk4_substeps","commands_recomputed_inside_rk4",
            "outer_control_cadence_changed","fuel_event_search_changed","contact_search_changed"}
        and type(trace.get("rk4_calls")) is int and trace["rk4_calls"] > 0
        and type(trace.get("rk4_substeps")) is int and trace["rk4_substeps"] == trace["rk4_calls"]*policy.subdivisions
        and type(trace.get("commands_recomputed_inside_rk4")) is int and trace["commands_recomputed_inside_rk4"] == 0
        and all(trace.get(key) is False for key in ("outer_control_cadence_changed","fuel_event_search_changed","contact_search_changed"))
        and contract._same(receipt,expected), "Restored integration receipt, completed status or held-command counters differ")


def _prefix_physical(launch, profile, *, settings, controls, expected_settings, expected_site, expected_goal,
                     expected_return_sites, expected_prefix_horizon_s):
    """Unchanged ground/ascent/split/deployment/Booster checks for explicit wrappers."""
    saved._json_tree({key:value for key,value in launch.items() if key != "ascent_control_trace"})
    _require(launch.get("scenario") == "experimental_physical_mission" and not any(key in launch for key in (*FORBIDDEN,"experimental_mission_execution"))
        and launch.get("development_return_qualification") == development_settings(), "Prefix borrowed old execution, certificate or actor scope")
    metadata = launch["experimental_mission_ascent"]
    contract._settings(metadata,controls)
    _require(metadata.get("settings") == expected_settings and metadata.get("requested_absolute_duration_s") == expected_prefix_horizon_s
        and metadata.get("shutdown_schedule") == settings.ascent.record()["shutdowns"]
        and metadata.get("final_mission_perigee_gate_m") == 220000.
        and metadata.get("booster_policy") == "availability_terminal_recovery_v1" and metadata.get("booster_terminal_recovery") == controls
        and metadata.get("booster_splashdown_goal") == expected_goal and metadata.get("booster_return_sites") == expected_return_sites
        and type(expected_return_sites) is dict and expected_site in expected_return_sites["sites"], "Prefix settings, source site or finite horizon differs")
    for key,value in (("engineering_objectives_not_real_flight_identification",True),("shared_production_deployment_and_return_loop",True),
            ("concurrent_actual_separation",True),("qualified_return",False),("mission_authority_granted",False),
            ("physical_execution",False),("state_reset",False),("all_features_completed",False)):
        _require(metadata.get(key) is value,"Prefix claim boundary changed")
    _require(type(metadata.get("native_model_calls")) is int and metadata["native_model_calls"] == 0,
        "Numerical ascent cannot claim native model work")
    evidence = launch["restored_prefix"]
    _require(evidence.get("schema") == "missionos.starship_restored_prefix.v1" and type(evidence.get("closed")) is bool
        and type(evidence.get("release_generation")) is int and evidence["release_generation"] == launch["outcome"]["payload_released_count"]
        and evidence.get("state_reset") is False and evidence.get("old_certificate_admitted") is False
        and evidence.get("mission_authority_granted") is False and evidence.get("model_calls_inferred") is False
        and evidence.get("origin_validation_issue") is None
        and evidence.get("decision_window_s") == 35.,"Restored prefix evidence claim boundary changed")
    _,last,outcome,events = _prefix_basics(launch,profile)
    _require(outcome["termination"] != "numerical_failure" and last["time_s"] <= expected_prefix_horizon_s,"Invalid numerical prefix or horizon")
    retained_policy = launch.get("retained_return",{})
    _require(not events.get("return_requested") and retained_policy.get("policy_id") == TRIMMED_POLICY_ID
        and retained_policy.get("status") == "not_activated" and retained_policy.get("activation") is None
        and retained_policy.get("landing_verified") is False and retained_policy.get("starship_vehicle_validated") is False
        and all(sample["phase"] in state_checks.prefix.PHASES | {"orbital_coast"} for sample in launch["samples"]),
        "Every restored prefix must stop before any return request, activation or return phase")
    reached,commands = state_checks._ascent(launch,profile,settings,metadata,events)
    saved._payloads(launch,profile,"$.prefix",[len(launch["samples"])],events,reached)
    releases = events.get("payload_released",[])
    _observations(launch["samples"],lambda sample:_vehicle_for_sample(launch,profile,sample,releases),settings)
    _clock(launch,profile,expected_prefix_horizon_s)
    state_checks._trace_clock_binding(launch)
    booster = launch.get("booster_run")
    booster_success, water = False, None
    _require(outcome.get("booster_return_invoked") is (booster is not None)
        and bool(events.get("stage_separation")) is (booster is not None),"Prefix actual Booster invocation differs")
    if booster is not None:
        booster_checks._booster(booster,launch["booster_separation_state"],profile,expected_site,expected_goal,expected_prefix_horizon_s,[0],
            settings=controls,checkpoint_required=True,availability_check=lambda sample,kind:state_checks._check_availability(sample,kind,settings))
        from .starship_splashdown_verifier import verify_splashdown
        water = verify_splashdown(booster,expected_goal)
        _require(water["passed"],"Independent Booster water geometry rejected")
        booster_success = water["controlled_water_entry_envelope_met"]
    return reached,commands,outcome,booster_success,water


def verify_restored_mission(record, profile, *, expected_source_sha256, expected_settings, expected_control_settings,
        expected_site, expected_goal, expected_return_sites, expected_prefix_horizon_s, expected_return_candidate_id,
        expected_total_horizon_s, expected_plan_sha256, expected_case_id, expected_integration_policy=None):
    """A well-formed failed physical result passes records, never success gates."""
    result = {"schema":"missionos.starship_restored_mission_verification.v1","passed":False,"issues":[],
        "physical_success":False,"ship_physical_success":False,"booster_physical_success":False,
        "old_certificate_admitted":False,"qualification_refreshed":False,"ai_control_completed":False,
        "all_features_completed":False,"physical_reexecution":False,"physical_execution":False,
        "model_work_evaluated":False,"scope":"saved actual state/commands/inventory/clock/contact consistency; no independent unsampled dynamics or authenticated authority proof"}
    try:
        contract._manifest(expected_source_sha256)
        _require({"src/runtime/starship_restored_prefix.py","src/runtime/starship_restored_return.py",
            "src/runtime/starship_restored_verifier.py"} <= expected_source_sha256.keys(),"Restored production source inventory missing")
        profile_raw = _expected_profile(profile)
        settings = state_checks._settings(expected_settings,profile)
        controls = booster_checks._settings(expected_control_settings)
        _require(contract._hash(expected_plan_sha256) and type(expected_case_id) is str and 0 < len(expected_case_id) <= 100
            and expected_return_candidate_id in ("nominal","next_orbit")
            and type(expected_prefix_horizon_s) in (int,float) and 3336. < expected_prefix_horizon_s <= 7500.
            and type(expected_total_horizon_s) in (int,float) and expected_total_horizon_s == 16000., "Explicit registered source/plan/case/horizons required")
        integrated = expected_integration_policy is not None
        keys = {"schema","execution","prefix","return"} | ({"numerical_integration"} if integrated else set())
        _require(type(record) is dict and set(record) == keys
            and record["schema"] == (INTEGRATED_SCHEMA if integrated else SCHEMA), "New restored wrapper schema or fields differ")
        if integrated:
            _require("src/runtime/starship_restored_integration.py" in expected_source_sha256,
                "Restored integration source inventory missing")
            _integration_receipt(record,expected_integration_policy)
            result.update(schema="missionos.starship_restored_mission_verification.v2",
                integration_policy=expected_integration_policy.record(), integration_execution_checked=True)
        wanted = {"schema":INTEGRATED_EXECUTION_SCHEMA if integrated else EXECUTION_SCHEMA,"plan_sha256":expected_plan_sha256,"case_id":expected_case_id,
            "source_sha256":expected_source_sha256,"profile_sha256":contract.PROFILE_SHA256,
            "settings_sha256":digest(expected_settings),"control_settings_sha256":digest(controls),
            "prefix_horizon_s":expected_prefix_horizon_s,"total_horizon_s":expected_total_horizon_s,
            "return_candidate_id":expected_return_candidate_id}
        if integrated:
            wanted["integration_policy"] = expected_integration_policy.record()
        _require((contract._same(record["execution"],wanted) if integrated else record["execution"] == wanted),
            "Registered execution source/profile/plan/settings binding differs")
        launch = record["prefix"]
        reached,commands,outcome,booster_success,water = _prefix_physical(launch,profile,
            settings=settings,controls=controls,expected_settings=expected_settings,expected_site=expected_site,
            expected_goal=expected_goal,expected_return_sites=expected_return_sites,
            expected_prefix_horizon_s=expected_prefix_horizon_s)
        if water is not None:
            result["booster"] = water
        if outcome["termination"] != "restored_deployment_handoff":
            _require(record["return"] is None and launch["restored_prefix"].get("closed") is False
                and launch["restored_prefix"].get("origin") is None,"Unclosed or failed prefix dispatched a return")
            result.update(passed=True,booster_physical_success=booster_success,termination=outcome["termination"],
                measured_final_insertion_gate_reached=reached,verified_ascent_command_samples=commands)
            return result
        origin = _origin(launch,profile_raw,expected_source_sha256)
        _decisions(launch,launch["restored_prefix"]["handoff_record"],profile)
        contact = _return(record["return"],origin,profile,settings,expected_return_candidate_id,expected_total_horizon_s)
        success = contact["contact_envelope_met"]
        result.update(passed=True,physical_success=bool(success and booster_success),ship_physical_success=success,
            booster_physical_success=booster_success,inventory_closed=True,retained_count=origin.record()["retained_count"],
            payload_released_count=origin.record()["released_count"],deployment_completed=origin.record()["retained_count"] == 0,
            verified_ascent_command_samples=commands,ship_contact=contact,return_candidate_id=expected_return_candidate_id,
            termination=record["return"]["outcome"]["termination"])
    except saved._Invalid as error:
        result["issues"].append(error.issue)
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError,ZeroDivisionError,StopIteration,OSError) as error:
        result["issues"].append({"code":"invalid_restored_mission_record","detail":str(error)})
    return result

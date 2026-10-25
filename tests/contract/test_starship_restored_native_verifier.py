"""Saved synthetic native evidence; never a flight, forecast, model or ledger."""
from copy import deepcopy
from dataclasses import replace
import gzip
from hashlib import sha256
import math
from pathlib import Path
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_restored_native_verifier as native
from src.runtime import starship_restored_verifier as physical
from src.runtime import starship_restored_admission as admission
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_return as returning
from src.runtime.starship_restored_approval import RestoredApprovalBinding
from src.runtime.starship_restored_approval_admission import RestoredApprovalAdmissionBridge
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
from tests.contract.test_starship_restored_admission import case, candidate, current
from tests.contract.test_starship_restored_approval_admission import fresh_forecast
from tests.contract.test_starship_restored_verifier import valid_negative as prior_negative, contact_fixture


@pytest.fixture(autouse=True)
def saved_only(monkeypatch):
    from src.intelligence.starship_restored_agent import RestoredAgent
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Native saved checker cannot run dynamics/controllers/models/processes")
    for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
            (mission, "advance_plant"), (mission, "simulate_concurrent"), (returning, "advance_plant"),
            (returning, "control"), (returning, "forecast_restored_return"), (RestoredAgent, "assess"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def integration_receipt(n=2):
    policy = RestoredIntegrationPolicy(n)
    return {"schema": "missionos.starship_restored_integration_execution.v1", "policy": policy.record(),
        "status": "completed", "failure_class": None, "trace": {"rk4_calls": 10, "rk4_substeps": 10*n,
            "commands_recomputed_inside_rk4": 0, "outer_control_cadence_changed": False,
            "fuel_event_search_changed": False, "contact_search_changed": False},
        "process_isolation_required": True, "internal_rk4_only": True, "physics_limits_changed": False,
        "control_policy_changed": False, "experimental": True, "qualification_refreshed": False,
        "mission_authority_granted": False, "execution_permission_granted": False,
        "hosted_model_calls": 0, "nested_sixdof_forecasts": 0}


def operations(sites=None, notice=None):
    sites = sites or [admission.RestoredSitePolicy("synthetic", 0., 0., 100000., 0., 16000.).record()]
    notice = notice or admission.RestoredOperationalNotice(0, 0., 16000., tuple(row["site_id"] for row in sites)).record()
    return {"schema": "missionos.starship_restored_native_operations.v1", "sites": sites,
        "notice_schedule": [{"notice": notice, "summary": "Registered synthetic operational notice."}],
        "deployment_notices": {"0": "Registered release slot0.", "13": "Registered release slot13."},
        "resource_accounting": {"epoch": "closed_origin", "initial_energy_kwh": 22., "power_kw": 6.,
            "initial_temperature_k": 295., "warming_k_per_s": .001,
            "initial_modeled_propellant_loss_kg": 0., "modeled_loss_kg_per_s": .02}}


def snapshot(observation):
    row = observation.record()
    return {"state": row["state"], "vehicle": row["vehicle"], "phase": "orbital_coast",
        "binding": row["binding"], "return_controller": None, "completed": False}


@pytest.fixture
def negative(monkeypatch):
    old, profile, expected = prior_negative.__wrapped__(monkeypatch)
    source = deepcopy(expected["expected_source_sha256"])
    for name in ("starship_restored_native_verifier.py", "starship_restored_coordinator.py", "starship_restored_integration.py"):
        source["src/runtime/"+name] = "c"*64
    context = {"run_id": "native_saved", "mission_id": "synthetic", "vehicle_id": "ship"}
    policy = RestoredIntegrationPolicy(2)
    host = {"schema": native.HOST_SCHEMA, "context": context, "plan_sha256": expected["expected_plan_sha256"],
        "source_sha256": native._hash(source), "mode": "fixture", "integration_policy": policy.record(),
        "decision_host": {"requests": []}}
    host_raw = native._wire(host)
    profile_raw = (Path(__file__).resolve().parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    wanted = {"schema": native.EXECUTION_SCHEMA, "context": context,
        "plan_sha256": expected["expected_plan_sha256"], "case_id": expected["expected_case_id"],
        "source_sha256": source, "profile_sha256": contract.PROFILE_SHA256,
        "settings_sha256": native._hash(expected["expected_settings"]),
        "control_settings_sha256": native._hash(expected["expected_control_settings"]),
        "prefix_horizon_s": 7500., "total_horizon_s": 16000., "initial_return_candidate_id": "nominal",
        "integration_policy": policy.record(), "mode": "fixture"}
    record = {"schema": native.SCHEMA, "execution": wanted, "prefix": old["prefix"], "return": None,
        "numerical_integration": integration_receipt(), "governance": None}
    args = {key: value for key, value in expected.items() if key != "expected_return_candidate_id"}
    args.update(expected_source_sha256=source, expected_context=context, expected_integration_policy=policy,
        artifacts={contract.PROFILE_SHA256: profile_raw}, host_record_raw=host_raw,
        expected_host_record_sha256=sha256(host_raw).hexdigest(), expected_mode="fixture", expected_operations=operations())
    return record, profile, args


def test_full_saved_ground_negative_is_valid_but_not_native_or_physical_success(negative, monkeypatch):
    record, profile, args = negative
    original = deepcopy(record)
    monkeypatch.setattr(Path, "read_bytes", lambda *_a: pytest.fail("saved native checker must use artifact map"))
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["termination"] == "angular_rate_envelope_exceeded"
    assert result["physical_success"] is result["judgment_chain_verified"] is result["ai_control_completed"] is False
    assert result["host_signature_verified_by_saved_checker"] is False
    assert result["operator_identity_authenticated_by_saved_checker"] is False
    assert record == original


@pytest.mark.parametrize("change", ["fixture_schema", "source", "context", "plan", "profile", "mode",
    "host_pin", "policy4", "counter", "bool_counter", "grant", "return", "missing_ops", "sites", "summary_type", "ops_resource"])
def test_expected_native_bindings_and_policy_gate_do_not_borrow_fixture_evidence(negative, change):
    record, profile, args = negative
    if change == "fixture_schema":
        record["schema"] = physical.INTEGRATED_SCHEMA
    elif change in ("source", "context", "plan", "profile", "mode"):
        key = {"source": "source_sha256", "context": "context", "plan": "plan_sha256", "profile": "profile_sha256", "mode": "mode"}[change]
        record["execution"][key] = "forged"
    elif change == "host_pin":
        args["expected_host_record_sha256"] = "e"*64
    elif change == "policy4":
        args["expected_integration_policy"] = RestoredIntegrationPolicy(4)
    elif change in ("counter", "bool_counter"):
        record["numerical_integration"]["trace"]["rk4_calls"] = 11 if change == "counter" else True
    elif change == "grant":
        record["numerical_integration"]["mission_authority_granted"] = True
    elif change == "return":
        record["return"] = {"pretend_completed": True}
    elif change == "missing_ops":
        args.pop("expected_operations")
    elif change == "sites":
        args["expected_operations"]["sites"][0]["radius_m"] = 500001.
    elif change == "summary_type":
        args["expected_operations"]["deployment_notices"]["0"] = True
    else:
        args["expected_operations"]["resource_accounting"]["initial_energy_kwh"] = 100.
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and result["issues"]


def test_old_fixture_verifier_still_rejects_native_wrapper(negative):
    record, profile, args = negative
    expected = {key: args[key] for key in ("expected_source_sha256", "expected_settings", "expected_control_settings",
        "expected_site", "expected_goal", "expected_return_sites", "expected_prefix_horizon_s",
        "expected_total_horizon_s", "expected_plan_sha256", "expected_case_id", "expected_integration_policy")}
    assert not physical.verify_restored_mission(record, profile, expected_return_candidate_id="nominal", **expected)["passed"]


def return_telemetry_case(latitude_deg=89.9, altitude_m=20.):
    """Invented finite return-state facts, not a propagated trajectory."""
    values = case()
    original, source, reservation = values[1], values[4], values[9].record()
    context = {"run_id": "telemetry", "mission_id": "synthetic", "vehicle_id": "ship"}
    plan, policy = "a"*64, RestoredIntegrationPolicy(2)
    bound = RestoredApprovalBinding(original, context=context, plan_sha256=plan,
        expected_source_sha256=source, started_wall_s=100.).record()
    raw = deepcopy(original.record()["state"])
    raw["time_s"] = reservation["return_time_s"]+.25
    raw["r_eci_m"] = list(env.ecef_to_eci(env.geodetic_to_ecef(math.radians(latitude_deg), .4, altitude_m), raw["time_s"]))
    craft = dyn.vehicle_from_dict(original.record()["vehicle"])
    outages = {row["engine_name"] for row in original.record()["ascent_settings"]["ascent"]["shutdowns"]
        if row["engine_name"].startswith("ship_")}

    def frame(state, index, started, phase):
        observation = {"state_sha256": native._hash(state), "simulation_time_s": state["time_s"], "wall_time_s": 100.+index}
        return {"received_wall_s": 100.+index, "frame": {"schema": "missionos.starship_restored_worker_telemetry.v1",
            "context": context, "plan_sha256": plan, "source_sha256": native._hash(source), "sequence": index,
            "runtime_state": {"run_active": True, "simulation_time_s": state["time_s"], "restored_origin_sha256": native._hash(original.record()),
                "revision": 0, "scope": bound["initial_scope"], "reservation_sha256": native._hash(reservation), "observation": observation},
            "actual_state": deepcopy(state), "phase": phase, "vehicle_sha256": native._hash(original.record()["vehicle"]),
            "binding_sha256": native._hash(reservation), "return_controller_started": started,
            "integration_policy_sha256": native._hash(policy.record()), "state_assigned": False}}

    host = {"telemetry": [frame(original.record()["state"], 1, False, "orbital_coast"), frame(raw, 2, True, "landing_burn")]}
    arguments = {"context": context, "plan_sha256": plan, "source": source, "policy": policy, "binding": bound}
    return raw, craft, outages, host, original, reservation, arguments, frame


@pytest.mark.parametrize("latitude,altitude", [(0., 0.), (0., 20.), (22., 20.), (89.9, 20.), (-90., 20.)])
def test_postcontroller_surface_telemetry_uses_generic_finite_state_not_orbital_radius(latitude, altitude):
    raw, craft, outages, host, original, reservation, arguments, _ = return_telemetry_case(latitude, altitude)
    before = deepcopy(raw)
    actual = native._return_telemetry_state(raw, craft, outages)
    assert actual.time_s == raw["time_s"] and raw == before
    assert env.ecef_to_geodetic(env.eci_to_ecef(actual.r_eci_m, actual.time_s))[2] == pytest.approx(altitude, abs=1e-7)
    assert len(native._telemetry(host, original, reservation, reservation, [], **arguments)) == 2
    if abs(latitude) >= 22.:
        assert env.norm(raw["r_eci_m"]) < env.EARTH_EQUATORIAL_RADIUS_M
        with pytest.raises(ValueError, match="invalid_attitude_or_position"):
            contract._state(raw, craft, outages)  # Orbital origin/coast guard remains unchanged.


@pytest.mark.parametrize("change", ["zero", "tiny", "nan", "vector", "quaternion", "engine_bool", "availability", "missing_engine",
    "throttle", "gimbal", "flap", "fuel", "bool_time", "missing_field"])
def test_generic_return_telemetry_remains_strict_for_complete_finite_hardware_state(change):
    raw, craft, outages, *_ = return_telemetry_case()
    if change in ("zero", "tiny"):
        raw["r_eci_m"] = [0., 0., 0.] if change == "zero" else [.1, .1, .1]
    elif change == "nan":
        raw["r_eci_m"][0] = float("nan")
    elif change == "vector":
        raw["v_eci_mps"] = [0., 1.]
    elif change == "quaternion":
        raw["q_body_to_eci"] = [0., 0., 0., 0.]
    elif change == "engine_bool":
        raw["engine_states"][0]["available"] = 1
    elif change == "availability":
        raw["engine_states"][0]["available"] = not raw["engine_states"][0]["available"]
    elif change == "missing_engine":
        raw["engine_states"].pop()
    elif change == "throttle":
        raw["engine_states"][0]["throttle"] = 1.1
    elif change == "gimbal":
        raw["engine_states"][0]["gimbal_x_rad"] = 2.
    elif change == "flap":
        raw["flap_angles_rad"][0] = 2.
    elif change == "fuel":
        raw["propellant_kg"] = craft.propellant_capacity_kg+1.
    elif change == "bool_time":
        raw["time_s"] = True
    else:
        raw.pop("omega_body_rad_s")
    with pytest.raises(ValueError):
        native._return_telemetry_state(raw, craft, outages)


@pytest.mark.parametrize("change", ["coast_flag", "coast_phase", "before_due", "rollback", "bool_started"])
def test_generic_return_state_cannot_bypass_coast_phase_reservation_time_or_sticky_controller(change):
    raw, _craft, _outages, host, original, reservation, arguments, frame = return_telemetry_case()
    if change == "coast_flag":
        host["telemetry"][1]["frame"]["return_controller_started"] = False
    elif change == "coast_phase":
        host["telemetry"][1]["frame"]["phase"] = "orbital_coast"
    elif change == "before_due":
        raw["time_s"] = original.time_s+.25
        host["telemetry"][1] = frame(raw, 2, True, "deorbit_slew")
    elif change == "bool_started":
        host["telemetry"][1]["frame"]["return_controller_started"] = 1
    else:
        later = deepcopy(original.record()["state"])
        later["time_s"] = raw["time_s"]+.25
        host["telemetry"].append(frame(later, 3, False, "orbital_coast"))
    with pytest.raises(ValueError):
        native._telemetry(host, original, reservation, reservation, [], **arguments)


def test_finite_crash_negative_state_does_not_acquire_a_new_telemetry_altitude_acceptance_gate():
    raw, craft, outages, host, original, reservation, arguments, _ = return_telemetry_case(22., -10.)
    assert env.ecef_to_geodetic(raw["r_eci_m"])[2] < 0
    native._return_telemetry_state(raw, craft, outages)
    assert len(native._telemetry(host, original, reservation, reservation, [], **arguments)) == 2
    # State integrity does not return contact, physical-success or authority flags.


def test_artifact_refs_are_rehashed_and_gzip_restored_without_path_access(monkeypatch):
    raw = b'{"fixture":true}'
    packed = gzip.compress(raw, mtime=0)
    reference = {"path": "forecasts/nominal/prediction.json.gz", "sha256": sha256(packed).hexdigest(), "raw_sha256": sha256(raw).hexdigest()}
    monkeypatch.setattr(Path, "open", lambda *_a, **_kw: pytest.fail("no artifact path opens"))
    assert native._artifact(reference, {reference["sha256"]: packed}, packed=True) == raw
    for kind in ("missing", "hash", "raw", "absolute", "traversal"):
        changed, artifacts = dict(reference), {reference["sha256"]: packed}
        if kind == "missing":
            artifacts = {}
        elif kind == "hash":
            artifacts[reference["sha256"]] += b"trailing"
        elif kind == "raw":
            changed["raw_sha256"] = "0"*64
        else:
            changed["path"] = "/private/origin.json" if kind == "absolute" else "../origin.json"
        with pytest.raises(ValueError):
            native._artifact(changed, artifacts, packed=True)


@pytest.mark.parametrize("candidate_id,stage,offset", [("nominal", "selection", 0.),
    ("next_orbit", "selection", 1.), ("nominal", "dispatch", 0.), ("nominal", "dispatch", .1)])
def test_independent_saved_admission_matches_real_typed_admission(candidate_id, stage, offset):
    values = case(candidate_id=candidate_id)
    selected = candidate(values)
    when = values[1].time_s+offset if stage == "selection" else selected.record()["proposal"]["return_time_s"]+offset
    observed = current(values, time_s=when)
    actual = admission.admit_restored_candidate(selected, observed, notice=values[7], resources=values[8], stage=stage)
    measured = native._saved_admission(selected, snapshot(observed), values[1], values[7], values[8], stage)
    assert measured == actual


def test_resource_and_notice_input_rows_must_follow_external_operations_actual_epoch():
    values = case()
    selected = candidate(values)
    ops = operations([row.record() for row in values[6]], values[7].record())
    native._operations_row(selected.record(), values[1].time_s, ops, values[1])
    for change in ("future", "old_generation", "site", "battery"):
        row = selected.record()
        if change == "future":
            row["resources"]["observed_s"] += 1.
        elif change == "old_generation":
            row["notice"]["generation"] += 1
        elif change == "site":
            row["sites"][0]["radius_m"] += 1.
        else:
            row["resources"]["energy_kwh"] -= .1
        with pytest.raises(ValueError):
            native._operations_row(row, values[1].time_s, ops, values[1])


def test_governance_inhibited_return_is_native_only_and_preserves_actual_coast():
    record, origin, profile, settings, _ = contact_fixture()
    state = origin.record()["state"]
    sample = deepcopy(record["samples"][0])
    sample["contact"] = False
    record.update(final_state=deepcopy(state), samples=[sample], return_controller=None, return_start_state=None,
        events=[record["events"][0], {"event": "return_inhibited_by_governance", "time_s": state["time_s"], "reason": "forecast_unavailable"}])
    record["outcome"] = {"termination": "return_inhibited_by_governance", "phase": "orbital_coast",
        "integration_steps": 0, "control_ticks": 1, "contact_receipt": None,
        "contact_metrics": {"contact_observed": False, "contact_envelope_met": False, "gates": {}}}
    binding = record["binding"]
    metrics = physical._return_physical(record, origin, profile, settings, 16000., initial_binding=binding,
        final_binding=binding, reservation_changes=[], allow_governance_stop=True)
    assert metrics["contact_envelope_met"] is False
    with pytest.raises(physical.saved._Invalid):
        physical._return(record, origin, profile, settings, "nominal", 16000.)
    record["samples"][0]["phase"] = "deorbit_burn"
    with pytest.raises(physical.saved._Invalid):
        physical._return_physical(record, origin, profile, settings, 16000., initial_binding=binding,
            final_binding=binding, reservation_changes=[], allow_governance_stop=True)


def test_zero_changes_reconstructs_initial_nominal_without_patching_record():
    values = case()
    binding = values[9].record()
    governance = {"initial_reservation": {"request": binding, "sha256": native._hash(binding)},
        "opportunity_anchor": values[2].opportunity_anchor_record(), "current_origin_changes": []}
    record = {"initial_binding": binding, "binding": binding, "reservation_changes": []}
    before = deepcopy(record)
    initial, final, changes = native._changes(governance, record, values[1], {}, values[3], values[4], {}, operations())
    assert initial == final == binding and not changes and record == before
    record["reservation_changes"] = [{"forged": "unreviewed change"}]
    with pytest.raises(ValueError):
        native._changes(governance, record, values[1], {}, values[3], values[4], {}, operations())


def test_one_change_uses_complete_actual_current_origin_and_stable_admission_not_prediction_state():
    nominal, later = case(), case(candidate_id="next_orbit")
    origin = nominal[1]
    profile_raw, source = nominal[3:5]
    _, actual_origin, actual_state = fresh_forecast(later)
    request = returning.RestoredReturnRequest(actual_origin,
        contract.RestoredCandidateSchedule(actual_origin, anchor=nominal[2]), "next_orbit", profile_raw, source)
    binding = nominal[9].record()
    observed = current(nominal, state_change=lambda row: row.update(state=deepcopy(actual_state)))
    before = snapshot(observed)
    sites = (replace(nominal[6][0], site_id="nominal_area"), replace(later[6][0], site_id="later_area"))
    notice = replace(nominal[7], available_site_ids=("nominal_area", "later_area"))
    elapsed = actual_origin.time_s-origin.time_s
    resources = replace(nominal[8], observed_s=actual_origin.time_s,
        energy_kwh=22.-elapsed*6./3600., temperature_k=295.+elapsed*.001, modeled_propellant_loss_kg=elapsed*.02)
    chosen = candidate(later, sites=sites, notice=notice, resources=resources)
    fallback = candidate(nominal, sites=sites, notice=notice, resources=resources)
    bound = RestoredApprovalBinding(origin, context={"run_id": "change", "mission_id": "fixture", "vehicle_id": "ship"},
        plan_sha256="a"*64, expected_source_sha256=source, started_wall_s=100.)
    bridge = RestoredApprovalAdmissionBridge(bound, chosen, fallback,
        current_inputs=lambda *_a: (chosen, observed, notice, resources))
    admission_row = bridge("select_next_orbit", {"run_active": True, "simulation_time_s": actual_origin.time_s,
        "restored_origin_sha256": bound.record()["restored_origin_sha256"]})
    change = {"time_s": actual_origin.time_s, "state": actual_state, "from_binding": binding,
        "to_binding": request.record(), "actual_state_assigned": False, "dispatch_authorized": False}
    packed_origin = actual_origin.source_record_bytes()
    ref = {"path": "reservation-change-origin.json", "sha256": sha256(packed_origin).hexdigest()}
    witness = {"origin": ref, "reservation_change_sha256": native._hash(change), "actual_snapshot": before,
        "candidate": chosen.record(), "admission": admission_row, "confirmation_snapshot": None}
    governance = {"initial_reservation": {"request": binding, "sha256": native._hash(binding)},
        "opportunity_anchor": nominal[2].opportunity_anchor_record(), "current_origin_changes": [witness],
        "decisions": [{"candidate": chosen.record()}]}
    record = {"initial_binding": binding, "binding": request.record(), "reservation_changes": [change]}
    forecasts = {later[0].sha256: (later[0], later[1], later[2])}
    args = (governance, record, origin, {ref["sha256"]: packed_origin}, profile_raw, source, forecasts,
        operations([row.record() for row in sites], notice.record()))
    first, last, witnesses = native._changes(*args)
    assert first == binding and last == request.record() and witnesses == [witness]
    record["reservation_changes"][0]["to_binding"]["return_time_s"] += 1.
    with pytest.raises(ValueError):
        native._changes(*args)


def synthetic_native_closed(monkeypatch, *, reject_next=False, live=False):
    """Orchestration smoke only: invented contacts and mocked ground proof.

    Actual prefix state/history, forecast artifacts, admission, host transport,
    handshake/telemetry, idle approval and dispatch checks are not mocked.
    """
    from src.runtime import starship_restored_decision_pipe as pipe
    from src.intelligence.starship_restored_agent import public_input
    first, later = case(), case(candidate_id="next_orbit")
    _, _, profile, settings, launch = contact_fixture()
    source = deepcopy(first[4])
    for name in ("starship_restored_native_verifier.py", "starship_restored_coordinator.py", "starship_restored_integration.py"):
        source["src/runtime/"+name] = "c"*64
    handoff = deepcopy(launch["restored_prefix"]["handoff_record"])
    handoff["source_sha256"] = source
    profile_raw = first[3]
    raw_origin = native._wire(handoff)
    origin = contract.ActualRestoredOrigin(sha256(raw_origin).hexdigest(), raw_origin, profile_raw, source)
    anchor = contract.RestoredCandidateSchedule(origin)
    requests = {key: returning.RestoredReturnRequest(origin, anchor, key, profile_raw, source) for key in ("nominal", "next_orbit")}
    launch["restored_prefix"].update(handoff_record=handoff, handoff_record_sha256=origin.source_record_sha256, origin=origin.record())
    launch["samples"] = [native._read(native._wire(mission._sample(dyn.state_from_dict(row["state"]),
        mission.vehicle(profile, payload_count=row["retained_count"]), "orbital_coast"))) for row in handoff["state_history"]]
    predictions = {}
    for key, values in (("nominal", first), ("next_orbit", later)):
        row = values[0].record()
        row.update(binding=requests[key].record(), initial_binding=requests[key].record())
        predictions[key] = returning.RestoredReturnResult(native._wire(row))
    context = {"run_id": "native_closed", "mission_id": "synthetic", "vehicle_id": "ship"}
    plan, policy = "a"*64, RestoredIntegrationPolicy(2)
    bound = RestoredApprovalBinding(origin, context=context, plan_sha256=plan, expected_source_sha256=source, started_wall_s=100.).record()
    artifacts = {contract.PROFILE_SHA256: profile_raw}
    def artifact(path, value, *, raw=False, packed=False):
        encoded = value if raw else native._wire(value)
        payload = gzip.compress(encoded, mtime=0) if packed else encoded
        digest = sha256(payload).hexdigest()
        artifacts[digest] = payload
        return {"path": path, "sha256": digest, **({"raw_sha256": sha256(encoded).hexdigest()} if packed else {})}
    origin_ref = artifact("closed-origin.json", raw_origin, raw=True)
    profile_ref = {"path": "forecasts/profile.json", "sha256": contract.PROFILE_SHA256}
    specs = [{"candidate_id": key, "request": request.record(), "schedule_chain": [{"origin_file": "origin-000.json",
        "origin_sha256": origin.source_record_sha256, "maximum_simulation_time_s": 16000., "schedule": anchor.record()}]} for key, request in requests.items()]
    job = {"schema": "missionos.starship_restored_forecast_batch.v1", "source_sha256": source,
        "integration_policy": policy.record(), "prediction_only": True, "execution_initialized": False,
        "model_calls": 0, "maximum_workers": 2, "origin_proves_numerical_policy": False,
        "matching_actual_policy_requires_outer_registered_execution_binding": True, "profile_file": "profile.json",
        "input_sha256": {"profile.json": contract.PROFILE_SHA256, "origin-000.json": origin.source_record_sha256}, "requests": specs}
    job_ref = artifact("forecasts/job.json", job)
    batch = {"job": job_ref, "batch_claim": artifact("forecasts/batch.claim", {"job_sha256": job_ref["sha256"], "attempt": 1}),
        "profile": profile_ref, "origins": {"origin-000.json": origin_ref}, "candidates": {}}
    for key, prediction in predictions.items():
        claim = {"job_sha256": job_ref["sha256"], "candidate_id": key, "request_sha256": native._hash(requests[key].record()), "attempt": 1}
        prediction_ref = artifact(f"forecasts/{key}/prediction.json.gz", prediction._record_json, raw=True, packed=True)
        receipt = integration_receipt()
        result = {"schema": "missionos.starship_restored_forecast_worker_result.v1", **claim,
            "raw_sha256": prediction.sha256, "gzip_sha256": prediction_ref["sha256"], "integration_execution": receipt,
            "prediction_only": True, "model_calls": 0, "execution_initialized": False}
        batch["candidates"][key] = {"prediction": prediction_ref, "result": artifact(f"forecasts/{key}/result.json", result),
            "integration": artifact(f"forecasts/{key}/integration.json", receipt),
            "started": artifact(f"forecasts/{key}/started.json", claim), "worker_claim": artifact(f"forecasts/{key}/worker.claim", claim)}
    sites = (replace(first[6][0], closes_s=6000.),) if reject_next else first[6]
    ops = operations([value.record() for value in sites], first[7].record())
    mode = "live" if live else "fixture"
    notice = first[7]
    def resource(when):
        elapsed = when-origin.time_s
        return admission.RestoredResourceState(0, when, 22.-elapsed*6./3600., 295.+elapsed*.001, elapsed*.02)
    def chosen(key, when):
        return admission.RestoredCandidate(predictions[key], origin, anchor, profile_raw, source,
            admission.RestoredAdmissionPolicy(), sites, notice, resource(when))
    binding = requests["nominal"].record()
    def snap(state):
        return {"state": deepcopy(state), "vehicle": origin.record()["vehicle"], "phase": "orbital_coast",
            "binding": binding, "return_controller": None, "completed": False}
    initial_snapshot = snap(origin.record()["state"])
    continuation = predictions["nominal"].record()
    continuation.update(schema="missionos.starship_restored_return_continuation.v1", prediction_only=False)
    dispatch_snapshot = snap(continuation["return_start_state"])
    due = dispatch_snapshot["state"]["time_s"]
    selected = chosen("nominal", due)
    dispatch_admission = native._saved_admission(selected, dispatch_snapshot, origin, notice, resource(due), "dispatch")
    assert dispatch_admission["accepted"], dispatch_admission
    actor = {"schema": "missionos.inflight_worker_runtime_record.v1", "context": context, "restored_binding": bound,
        "initial_revision": 0, "initial_scope": bound["initial_scope"], "bounds": bound["bounds"],
        "host_signature_verified_by_worker": False, "burn_dispatch_issued": False, "physical_return_verified": False,
        "request_frame": None, "status": "idle", "revision": 0, "scope": bound["initial_scope"], "host_result": None, "applied_reservation": None}
    ready = {"schema": "missionos.starship_restored_origin_ready.v1", "context": context, "plan_sha256": plan,
        "source_sha256": native._hash(source), "profile_sha256": contract.PROFILE_SHA256, "origin_file": origin_ref["path"],
        "source_record_sha256": origin.source_record_sha256, "origin_sha256": native._hash(origin.record()),
        "binding_sha256": native._hash(bound), "opportunity_anchor_sha256": native._hash(anchor.opportunity_anchor_record()),
        "initial_reservation_sha256": native._hash(binding), "integration_policy_sha256": native._hash(policy.record()),
        "started_wall_s": 100., "nonce": "f"*64}
    handshake = {"ready": ready, "ack": {"schema": "missionos.starship_restored_origin_ready_ack.v1", "ready_sha256": native._hash(ready),
        "nonce": ready["nonce"], "context": context, "plan_sha256": plan, "source_sha256": native._hash(source),
        "binding_sha256": native._hash(bound), "initial_reservation_sha256": native._hash(binding), "ledger_ready": True}}
    sessions, results, deployment = [], [], []
    def exchange(req, action):
        pipe._request(req)
        provider = {"provider": "typesafe", "request_input_sha256": native._hash(public_input(req, provider="typesafe")),
            "model_inference_invoked": False, "retries": 0, "mode": "synthetic_fixture", "status": "synthetic_decoder_only"}
        for flag in ("approval_granted", "execution_authorized", "dispatch", "physical_execution", "raw_prompt_recorded",
                "raw_response_recorded", "raw_reasoning_recorded", "call_attempted", "call_succeeded", "response_received", "complete_response_observed"):
            provider[flag] = False
        if live:
            # Invented native-looking protocol data: no provider is called.
            provider.update(mode="live", status="succeeded", model_inference_invoked=True,
                model_id="jev-1.13.0", request_model="jev-1.13.0", model_inference_status="confirmed_by_valid_response",
                request_sha256="a"*64, response_sha256="b"*64, request_bytes=100, response_bytes_read=100,
                call_attempted=True, call_succeeded=True, response_received=True, complete_response_observed=True)
        judgment = {"schema": "missionos.starship_restored_judgment.v1", "request_id": req["request_id"],
            "request_sha256": native._hash(req), "provider": "typesafe", "action": action, "status": "valid", "summary": "Synthetic only.",
            "provider_receipt": provider, "model_inference_invoked": live, "execution_permission_granted": False}
        attempt = {"provider": "typesafe", "request_sha256": native._hash(req), "decision_deadline_s": req["deadline_s"],
            "remaining_s": req["deadline_s"]-req["time_s"], "judgment": judgment}
        result = pipe._result(req, [attempt], judgment)
        frame = {"schema": pipe.REQUEST_FRAME, "context": context, "plan_sha256": plan, "source_sha256": native._hash(source),
            "sequence": len(sessions)+1, "request_id": req["request_id"], "request_sha256": native._hash(req), "request": req,
            "started_wall_s": 100., "wall_deadline_s": 100.+req["deadline_s"]-req["time_s"]}
        sessions.append({"frame": frame, "attempts": [attempt], "started_attempts": [{"provider": "typesafe"}],
            "thread_result": result, "delivered_result": result})
        results.append(result)
        return result, frame
    for i, actual in enumerate(launch["restored_prefix"]["decisions"]):
        when, count = actual["summary"]["time_s"], actual["summary"]["released_count"]
        req = {"schema": "missionos.starship_restored_decision_request.v1", "request_id": f"deploy-{i}", "phase": "deployment",
            "scope_sha256": native._hash(bound["initial_scope"]), "time_s": when, "deadline_s": when+35.,
            "facts": {"time_s": when, "released_count": count, "retained_count": 26-count},
            "notice_summary": ops["deployment_notices"][str(count)], "allowed_actions": ["continue_deployment", "withhold_deployment"]}
        result, frame = exchange(req, actual["action"])
        deployment.append({"request": req, "request_sha256": native._hash(req), "exchange": result, "actual_slot": count,
            "applied_action": actual["action"], "applied_at_actual_time_s": when, "fallback_used": False,
            "pacing": {"wall_start_s": 100., "wall_deadline_s": frame["wall_deadline_s"], "simulation_start_s": when,
                "simulation_deadline_s": when+35., "last_wall_s": 100.}})
    # Use the production request builder with static current snapshots and tools;
    # this exercises its costs/facts seam against the independent saved checker.
    from src.runtime.starship_restored_coordinator import RestoredReturnCoordinator
    from types import SimpleNamespace
    c = object.__new__(RestoredReturnCoordinator)
    c.tools = lambda _s: (notice, resource(origin.time_s))
    c._snapshot = lambda: deepcopy(initial_snapshot)
    c._state = lambda: {"simulation_time_s": origin.time_s, "revision": 0, "scope": bound["initial_scope"]}
    c._admit = lambda key, stage: (chosen(key, origin.time_s), native._saved_admission(chosen(key, origin.time_s), initial_snapshot,
        origin, notice, resource(origin.time_s), stage))
    c.binding = SimpleNamespace(record=lambda: deepcopy(bound))
    c.anchor, c.origin, c.clock, c.requests, c.changes = anchor, origin, lambda: 100., requests, []
    c._maximum_step = lambda: .25
    c.actor = SimpleNamespace(status="idle")
    c.generations, c.last_generation, c.notice_hashes, c.used_generations = (0,), -1, {}, set()
    c.summary = lambda _n: ops["notice_schedule"][0]["summary"]
    captured = []
    c.client = SimpleNamespace(begin=lambda req, *, started_wall_s=None: captured.append(deepcopy(req)) or {
        "started_wall_s": 100. if started_wall_s is None else started_wall_s,
        "wall_deadline_s": (100. if started_wall_s is None else started_wall_s)+req["deadline_s"]-req["time_s"]})
    c.sleep = lambda _s: None
    c._begin_judgment()
    req = captured[0]
    action = "select_next_orbit" if reject_next else "select_nominal"
    result, _ = exchange(req, action)
    selected_now = chosen("next_orbit" if reject_next else "nominal", origin.time_s)
    choice = {"request_id": req["request_id"], "original_action": action,
        "disposition": "valid_choice_rejected_not_overwritten" if reject_next else "valid_keep_reservation_unchanged",
        "actual_snapshot": initial_snapshot, "request_snapshot": initial_snapshot,
        "request_candidates": c._decision["request_candidates"], "candidate": selected_now.record(),
        "admission": native._saved_admission(selected_now, initial_snapshot, origin, notice, resource(origin.time_s), "selection")}
    shared = {"context": context, "plan_sha256": plan, "source_sha256": native._hash(source)}
    client = {"schema": "missionos.starship_restored_decision_client.v1", **shared, "pending_request": None,
        "provider_calls_by_worker": 0, "execution_permission_granted": False, "results": results}
    host = {"schema": native.HOST_SCHEMA, **shared, "mode": mode, "integration_policy": policy.record(), "handshake": handshake,
        "decision_host": {"schema": "missionos.starship_restored_decision_host.v1", **shared, "execution_permission_granted": False, "requests": sessions},
        "approval_host": {"binding": bound}, "operator_ledger": [], "telemetry": []}
    for index, snap_row in enumerate((initial_snapshot, dispatch_snapshot)):
        state = snap_row["state"]
        runtime = {"run_active": True, "simulation_time_s": state["time_s"], "restored_origin_sha256": native._hash(origin.record()),
            "revision": 0, "scope": bound["initial_scope"], "reservation_sha256": native._hash(binding),
            "observation": {"state_sha256": native._hash(state), "simulation_time_s": state["time_s"], "wall_time_s": 100.+index}}
        frame = {"schema": "missionos.starship_restored_worker_telemetry.v1", **shared, "sequence": index+1,
            "runtime_state": runtime, "actual_state": state, "phase": "orbital_coast", "vehicle_sha256": native._hash(origin.record()["vehicle"]),
            "binding_sha256": native._hash(binding), "return_controller_started": False,
            "integration_policy_sha256": native._hash(policy.record()), "state_assigned": False}
        host["telemetry"].append({"frame": frame, "received_wall_s": 100.+index})
    governance = {"schema": native.GOVERNANCE_SCHEMA, "integration_policy": policy.record(), "qualification_refreshed": False,
        "mission_authority_granted": False, "physical_execution": False, "closed_origin": {"raw": origin_ref, "origin_sha256": native._hash(origin.record())},
        "forecast_batches": [batch], "initial_reservation": {"request": binding, "sha256": native._hash(binding)},
        "opportunity_anchor": anchor.opportunity_anchor_record(), "current_origin_changes": [], "approval": {"binding": bound, "worker": actor},
        "handshake": handshake, "decision_client": client, "deployment": {"schema": "missionos.starship_restored_deployment_judgments.v1",
            "pending_request": None, "closed_inventory_inferred": False, "physical_release_inferred": False,
            "native_model_calls_inferred": False, "decisions": deployment}, "notice_generations": [0], "decisions": [choice],
        "dispatch": {"gate_met": True, "refusal_reason": None, "actual_snapshot": dispatch_snapshot, "candidate": selected.record(), "admission": dispatch_admission}}
    from tests.contract.test_starship_restored_verifier import scope
    from tests.contract.test_starship_restored_prefix import setup
    controls = scope(setup.__wrapped__())["expected_control_settings"]
    host_raw = native._wire(host)
    args = {"expected_context": context, "expected_plan_sha256": plan, "expected_case_id": "synthetic_closed",
        "expected_source_sha256": source, "expected_settings": settings.record(), "expected_control_settings": controls,
        "expected_site": {}, "expected_goal": {}, "expected_return_sites": {}, "expected_prefix_horizon_s": 7500.,
        "expected_total_horizon_s": 16000., "expected_integration_policy": policy, "artifacts": artifacts,
        "host_record_raw": host_raw, "expected_host_record_sha256": sha256(host_raw).hexdigest(),
        "expected_mode": mode, "expected_operations": ops}
    execution = {"schema": native.EXECUTION_SCHEMA, "context": context, "plan_sha256": plan, "case_id": args["expected_case_id"],
        "source_sha256": source, "profile_sha256": contract.PROFILE_SHA256, "settings_sha256": native._hash(settings.record()),
        "control_settings_sha256": native._hash(controls), "prefix_horizon_s": 7500., "total_horizon_s": 16000.,
        "initial_return_candidate_id": "nominal", "integration_policy": policy.record(), "mode": mode}
    record = {"schema": native.SCHEMA, "execution": execution, "prefix": launch, "return": continuation,
        "numerical_integration": integration_receipt(), "governance": governance}
    monkeypatch.setattr(physical, "_prefix_physical", lambda *_a, **_kw: (True, 7, {"termination": "restored_deployment_handoff"}, True, {"synthetic_ground_proof": True}))
    return record, profile, args


def test_closed_saved_orchestration_positive_full_artifacts_and_dispatch_is_not_native_flight_proof(monkeypatch):
    record, profile, args = synthetic_native_closed(monkeypatch)
    before = deepcopy(record)
    monkeypatch.setattr(Path, "read_bytes", lambda *_a: pytest.fail("saved checker must not read artifact paths"))
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["physical_success"] is result["ship_physical_success"] is True
    assert result["hosted_model_attempts"] == result["confirmed_native_inferences"] == 0
    assert result["judgment_chain_verified"] is result["ai_control_completed"] is False
    assert result["dispatch_admission_checked"] is True and result["reservation_change_count"] == 0
    assert record == before


@pytest.mark.parametrize("change", ["forecast_pair", "forecast_claim", "forecast_raw", "forecast_policy", "current_origin",
    "anchor", "host_ack", "telemetry_state", "telemetry_wall", "approval_revision", "dispatch_state", "dispatch_admission",
    "contact_gate", "notice_cost", "notice_repeat", "deployment_choice", "model_claim", "lost_host_attempt"])
def test_complete_closed_orchestration_rejects_bound_evidence_tampering(monkeypatch, change):
    record, profile, args = synthetic_native_closed(monkeypatch)
    governance = record["governance"]
    host = native._read(args["host_record_raw"])
    if change == "forecast_pair":
        del governance["forecast_batches"][0]["candidates"]["next_orbit"]
    elif change in ("forecast_claim", "forecast_raw", "forecast_policy"):
        node = governance["forecast_batches"][0]["candidates"]["nominal"]
        key = {"forecast_claim": "worker_claim", "forecast_raw": "prediction", "forecast_policy": "integration"}[change]
        args["artifacts"][node[key]["sha256"]] += b" "
    elif change == "current_origin":
        governance["closed_origin"]["origin_sha256"] = "0"*64
    elif change == "anchor":
        governance["opportunity_anchor"]["next_orbit_s"] += 1.
    elif change == "host_ack":
        host["handshake"]["ack"]["ledger_ready"] = False
    elif change == "telemetry_state":
        host["telemetry"][-1]["frame"]["actual_state"]["engine_states"][0]["available"] = False
    elif change == "telemetry_wall":
        host["telemetry"][-1]["received_wall_s"] += 6.
    elif change == "approval_revision":
        governance["approval"]["worker"]["revision"] = 1
    elif change == "dispatch_state":
        governance["dispatch"]["actual_snapshot"]["state"]["r_eci_m"][0] += 1.
    elif change == "dispatch_admission":
        governance["dispatch"]["admission"]["accepted"] = False
    elif change == "contact_gate":
        record["return"]["outcome"]["contact_metrics"]["gates"]["reserve"] = False
    elif change == "notice_cost":
        governance["decisions"][0]["request_candidates"]["next_orbit"]["resources"]["energy_kwh"] += .1
    elif change == "notice_repeat":
        governance["decisions"].append(deepcopy(governance["decisions"][0]))
    elif change == "deployment_choice":
        governance["deployment"]["decisions"][0]["applied_action"] = "withhold_deployment"
    elif change == "model_claim":
        governance["decision_client"]["results"][-1]["model_inference_invoked"] = True
    elif change == "lost_host_attempt":
        host["decision_host"]["requests"][-1]["attempts"] = []
    args["host_record_raw"] = native._wire(host)
    args["expected_host_record_sha256"] = sha256(args["host_record_raw"]).hexdigest()
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"], (change, result)


def test_native_scope_does_not_borrow_synthetic_provider_status_as_inference(monkeypatch):
    record, _profile, args = synthetic_native_closed(monkeypatch)
    host = native._read(args["host_record_raw"])
    session = host["decision_host"]["requests"][-1]
    req = session["frame"]["request"]
    answer = session["attempts"][0]["judgment"]
    assert answer["status"] == "valid"
    assert native._provider_evidence(answer, req, "typesafe", "fixture") == (0, 0)
    with pytest.raises(ValueError, match="borrowed_fixture"):
        native._provider_evidence(answer, req, "typesafe", "live")
    # Invented protocol receipt below is a type/field check, not an API call.
    live = deepcopy(answer)
    live["model_inference_invoked"] = live["provider_receipt"]["model_inference_invoked"] = True
    receipt = live["provider_receipt"]
    receipt.update(mode="live", status="succeeded", model_id="jev-1.13.0", request_model="jev-1.13.0",
        model_inference_status="confirmed_by_valid_response", request_sha256="a"*64,
        response_sha256="b"*64, request_bytes=100, response_bytes_read=100)
    for key in ("call_attempted", "call_succeeded", "response_received", "complete_response_observed"):
        receipt[key] = True
    assert native._provider_evidence(live, req, "typesafe", "live") == (1, 1)
    for key in ("call_attempted", "complete_response_observed", "request_model", "response_sha256"):
        changed = deepcopy(live)
        changed["provider_receipt"][key] = False if type(receipt[key]) is bool else "mismatch"
        with pytest.raises(ValueError):
            native._provider_evidence(changed, req, "typesafe", "live")


def test_synthetic_approval_chain_does_not_upgrade_content_hash_into_hmac_or_identity(monkeypatch):
    record, _profile, args = synthetic_native_closed(monkeypatch)
    governance = record["governance"]
    host = native._read(args["host_record_raw"])
    reference = governance["closed_origin"]["raw"]
    original = native._origin(reference, args["artifacts"], args["artifacts"][contract.PROFILE_SHA256], args["expected_source_sha256"])
    initial = record["return"]["initial_binding"]
    options = dict(context=args["expected_context"], plan_sha256=args["expected_plan_sha256"], source=args["expected_source_sha256"],
        inhibited=False, observations={})
    bound, allowed, amended = native._approval(governance, host, original, initial, initial, [], **options)
    assert allowed and not amended and bound == governance["approval"]["binding"]
    # A forged JSON scope revision with no retained operator consumption fails.
    governance["approval"]["worker"].update(revision=1, status="reservation_confirmed", scope=bound["registry"])
    with pytest.raises(ValueError):
        native._approval(governance, host, original, initial, initial, [], **options)


def test_decision_choice_set_is_fixed_by_actual_scope_not_a_producer_selected_single_answer(monkeypatch):
    record, _profile, args = synthetic_native_closed(monkeypatch)
    host = native._read(args["host_record_raw"])
    original = native._origin(record["governance"]["closed_origin"]["raw"], args["artifacts"],
        args["artifacts"][contract.PROFILE_SHA256], args["expected_source_sha256"])
    exchanges, _, _ = native._judgments(record["governance"]["decision_client"], host["decision_host"],
        context=args["expected_context"], plan_sha256=args["expected_plan_sha256"], source=args["expected_source_sha256"], mode="fixture")
    forecasts = native._forecast_batches(record["governance"]["forecast_batches"], args["artifacts"],
        args["artifacts"][contract.PROFILE_SHA256], args["expected_source_sha256"], args["expected_integration_policy"], original, host)
    key = record["governance"]["decisions"][0]["request_id"]
    exchanges[key]["request"]["allowed_actions"] = ["select_nominal"]
    with pytest.raises(ValueError, match="choice_set_changed"):
        native._return_decisions(record["governance"], exchanges, original, forecasts,
            args["artifacts"][contract.PROFILE_SHA256], args["expected_source_sha256"],
            record["governance"]["approval"]["binding"], args["expected_operations"])


@pytest.mark.parametrize("rejected", [False, True])
def test_main_native_receipt_chain_is_distinct_from_an_honored_final_return_choice(monkeypatch, rejected):
    # All provider data and contacts are invented; this tests claim arithmetic,
    # not actual inference, operator identity or flight performance.
    record, profile, args = synthetic_native_closed(monkeypatch, reject_next=rejected, live=True)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["physical_success"] is result["judgment_chain_verified"] is True
    assert result["confirmed_native_inferences"] == result["hosted_model_attempts"] == 3
    assert result["honored_native_return_choice"] is (not rejected)
    assert result["ai_control_completed"] is (not rejected)
    assert result["return_candidate_id"] == "nominal" and result["reservation_change_count"] == 0


@pytest.mark.parametrize("where,mutation", [("request_snapshot", "source_sha256"), ("request_snapshot", "return_time_s"),
    ("actual_snapshot", "source_sha256"), ("actual_snapshot", "state"), ("dispatch", "state")])
def test_detached_governance_snapshot_must_match_host_accepted_actual_state_and_reservation(monkeypatch, where, mutation):
    record, profile, args = synthetic_native_closed(monkeypatch)
    # Detach shared Python fixture aliases exactly as a wire JSON record would.
    record = native._read(native._wire(record))
    snapshot = record["governance"]["dispatch"]["actual_snapshot"] if where == "dispatch" else record["governance"]["decisions"][0][where]
    if mutation == "state":
        snapshot["state"]["r_eci_m"][0] += .01
    elif mutation == "return_time_s":
        snapshot["binding"][mutation] += 123.
    else:
        snapshot["binding"][mutation] = "f"*64
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"], result
    assert "host_accepted_state_and_reservation" in str(result["issues"]), result


def test_full_saved_orchestration_accepts_low_altitude_started_return_telemetry_without_changing_contact_checks(monkeypatch):
    """Ground proof remains mocked by the existing explicitly synthetic fixture."""
    record, profile, args = synthetic_native_closed(monkeypatch)
    host = native._read(args["host_record_raw"])
    late = deepcopy(host["telemetry"][-1])
    frame = late["frame"]
    raw = frame["actual_state"]
    raw["time_s"] += .25
    raw["r_eci_m"] = list(env.ecef_to_eci(env.geodetic_to_ecef(math.radians(22.), .4, 20.), raw["time_s"]))
    assert env.norm(raw["r_eci_m"]) < env.EARTH_EQUATORIAL_RADIUS_M
    frame.update(sequence=3, return_controller_started=True, phase="landing_burn")
    frame["runtime_state"].update(simulation_time_s=raw["time_s"], observation={"state_sha256": native._hash(raw),
        "simulation_time_s": raw["time_s"], "wall_time_s": 102.})
    late["received_wall_s"] = 102.
    host["telemetry"].append(late)
    args.update(host_record_raw=native._wire(host), expected_host_record_sha256=native._hash(host))
    contact_before = deepcopy(record["return"]["outcome"]["contact_metrics"])
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["ship_contact"] == contact_before
    assert result["physical_reexecution"] is result["operator_identity_authenticated_by_saved_checker"] is False


def retained_operator_chain(tmp_path):
    """Real local ledger/pipes with scripted trusted telemetry, no HTTP/plant."""
    from scripts.smoke_starship_restored_approval import Clock, Scenario, synthetic_origin
    from src.gateway.starship_inflight_approval import InflightApprovalService
    from tests.contract.test_starship_restored_approval import approve
    original, source = synthetic_origin()
    clock = Clock()
    value = Scenario("native-saved-approval", tmp_path, original, source, InflightApprovalService(clock=clock), clock)
    try:
        approve(value)
        value.worker.tick(value.state)
        value.observe(.1)
        value.worker.tick(value.state)
        actor, host = value.worker.record(), value.host.record()
        bound = value.bound.record()
        initial = {"fixture_nominal_reservation": "native-saved-approval", "origin_sha256": native._hash(original.record())}
        final = {"candidate": value.candidate, "revision": 1}
        state = original.record()["state"]
        later = deepcopy(state)
        later["time_s"] += .1
        confirmation = {"state": later, "vehicle": original.record()["vehicle"], "binding": final,
            "phase": "orbital_coast", "return_controller": None, "completed": False}
        changes = [{"actual_snapshot": {"state": state}, "confirmation_snapshot": confirmation,
            "admission": {"proposal_sha256": value.candidate, "accepted": True}}]
        ledger = value.host.ledger.status("change1")
        observations = {native._hash(actor["request_frame"]["observation"]): {},
            native._hash(ledger["consumption"]["latest_observation"]): {}}
        governance = {"approval": {"binding": bound, "worker": actor}}
        retained_host = {"approval_host": host, "operator_ledger": [ledger]}
        options = dict(context=bound["context"], plan_sha256=bound["plan_sha256"], source=source,
            inhibited=False, observations=observations)
        return governance, retained_host, original, initial, final, changes, options
    finally:
        value.close()


def test_independent_retained_operator_chain_matches_real_local_ledger_worker_consumption_and_later_confirmation(tmp_path):
    args = retained_operator_chain(tmp_path)
    governance, host, original, initial, final, changes, options = args
    bound, confirmed, amended = native._approval(governance, host, original, initial, final, changes, **options)
    assert confirmed is amended is True
    assert bound == governance["approval"]["binding"]
    # This saved consistency check deliberately never authenticates an HMAC key
    # or real human identity; the caller separately trusts the pinned host bytes.


@pytest.mark.parametrize("mutation", ["missing_consumption", "other_proposal", "other_receipt", "future_confirmation", "missing_host_observation"])
def test_retained_operator_consumption_or_confirmation_mismatch_cannot_admit_changed_reservation(tmp_path, mutation):
    governance, host, original, initial, final, changes, options = retained_operator_chain(tmp_path)
    if mutation == "missing_consumption":
        host["operator_ledger"][0]["consumption"] = None
    elif mutation == "other_proposal":
        changes[0]["admission"]["proposal_sha256"] = "d"*64
    elif mutation == "other_receipt":
        host["operator_ledger"][0]["consumption"]["receipt_sha256"] = "d"*64
    elif mutation == "future_confirmation":
        changes[0]["confirmation_snapshot"]["state"]["time_s"] = original.time_s
    else:
        options["observations"] = {}
    with pytest.raises(ValueError):
        native._approval(governance, host, original, initial, final, changes, **options)

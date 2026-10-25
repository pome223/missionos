"""Private, current-state sensor hypotheses for the maintained final mission.

Snapshots are trusted numerical inputs, NEVER provider observations. They bind
the actual rigid vehicle, closed release inventory, actuator state and complete
return-controller history. Main-engine model limits are reconstructed from the
frozen nominal profile; the execution vehicle's hidden thrust cannot choose a
hypothesis. Only a named point of an observation-derived band may do that.

These sampled, same-model forecasts grant no authority or qualification. The
caller owns a separate process lifetime bound. Use processes, not threads: the
shared separated controller audits module-level control boundaries.
"""
from dataclasses import asdict, fields, replace
from hashlib import sha256
import math
from pathlib import Path
import re
import sys
import time

from . import starship_ship_capability_guard as prior
from . import starship_ship_capability_observation as obs
from . import starship_sixdof as dyn
from .starship_return_feasibility import backend
from .starship_ship_fault_screen import AvailableCapacityController
from .starship_sixdof_mission import vehicle
from .starship_restored_integration import (RestoredIntegrationPolicy, restored_integration,
    validate_execution_receipt)

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_SCHEMA = "missionos.starship_final_sensor_snapshot.v1"
FORECAST_SCHEMA = "missionos.starship_final_sensor_forecast.v1"
SNAPSHOT_SCHEMA_V2 = "missionos.starship_final_sensor_snapshot.v2"
FORECAST_SCHEMA_V2 = "missionos.starship_final_sensor_forecast.v2"
INVENTORY_SCHEMA = "missionos.starship_restored_inventory.v1"
SCOPE = "current_state_discrete_sensor_hypotheses_not_qualification"
CONTINUE = "continue_nominal_assumption"
ADOPT = "adopt_accelerometer_estimate"
ACTIONS = (CONTINUE, ADOPT)
POINT_LABELS = ("band_low", "point_estimate", "band_high")
RETAINED_COUNTS = (0, 13, 26)
CONTACT_LIMITS = dict(prior.CONTACT_LIMITS)
REQUIRED_SOURCE = frozenset({"src/runtime/starship_final_sensor_prediction.py",
    "src/runtime/starship_ship_capability_observation.py",
    "src/runtime/starship_ship_capability_matrix.py", "src/runtime/starship_ship_fault_screen.py",
    "src/runtime/starship_ship_return.py", "src/runtime/starship_retained_return.py",
    "src/runtime/starship_sixdof.py", "src/runtime/starship_sixdof_mission.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_entry_trim.py",
    "src/runtime/starship_fin_allocation.py", "src/runtime/starship_attitude_reference.py",
    "src/runtime/starship_return_feasibility.py", "src/runtime/starship_ship_capability_guard.py",
    "src/runtime/starship_restored_integration.py"})
SNAPSHOT_KEYS = {"schema", "run_id", "handoff_sha256", "retained_count", "released_count",
    "cutoff_time_s", "complete_time_s", "application_time_s", "state", "actual_vehicle",
    "controller", "profile", "inventory", "state_sha256", "actual_vehicle_sha256",
    "nominal_vehicle_sha256", "controller_sha256", "actuator_sha256", "profile_sha256",
    "inventory_sha256", "inventory_receipt_chain_sha256", "snapshot_id"}
JOB_KEYS = {"snapshot", "action", "sample_label", "sampled_factor", "accelerometer_estimate",
    "sampling_estimate", "source_sha256", "backend", "timeout_s"}
FORECAST_KEYS = {"schema", "snapshot_id", "action", "sample_label", "sampled_factor",
    "assumed_factor", "application_time_s", "accelerometer_estimate_id", "sampling_estimate_id",
    "source_sha256", "backend", "record", "verification", "stronger_record_checks", "forecast_id"}
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def is_hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def json_copy(value):
    import json
    return json.loads(obs.encoded(value))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def integration_fields(policy=None):
    """Only omitted policy uses legacy v1 shapes and implicit strict_v1/N2.

    Any explicit typed policy uses a new fully bound v2 sensor wire contract,
    including strict_v1 policies with a different subdivision count.
    """
    if policy is None:
        return {}
    _require(type(policy) is RestoredIntegrationPolicy
        and RestoredIntegrationPolicy.from_record(policy.record()) == policy,
        "final_sensor_typed_integration_policy_required")
    return {"integration_policy": policy.record()}


def policy_for_snapshot(snapshot):
    _require(type(snapshot) is dict, "final_sensor_snapshot_integration_policy_required")
    if snapshot.get("schema") == SNAPSHOT_SCHEMA:
        _require("integration_policy" not in snapshot, "final_sensor_v1_policy_field_forbidden")
        return RestoredIntegrationPolicy(2)
    _require(snapshot.get("schema") == SNAPSHOT_SCHEMA_V2 and "integration_policy" in snapshot,
        "final_sensor_snapshot_integration_policy_required")
    return RestoredIntegrationPolicy.from_record(snapshot["integration_policy"])


def snapshot_integration_fields(snapshot):
    policy = policy_for_snapshot(snapshot)
    return {} if snapshot["schema"] == SNAPSHOT_SCHEMA else integration_fields(policy)


def _forecast_fields(value):
    _require(type(value) is dict, "final_sensor_forecast_identity_mismatch")
    explicit = value.get("schema") == FORECAST_SCHEMA_V2
    _require(value.get("schema") in (FORECAST_SCHEMA, FORECAST_SCHEMA_V2)
        and set(value) == FORECAST_KEYS | ({"integration_policy", "integration_execution"} if explicit else set()),
        "final_sensor_forecast_identity_mismatch")
    if explicit:
        policy = RestoredIntegrationPolicy.from_record(value["integration_policy"])
        receipt = validate_execution_receipt(value["integration_execution"], policy)
        _require(receipt == value["integration_execution"], "final_sensor_forecast_integration_receipt_changed")
    return explicit


def _actuators(state):
    return {key: state[key] for key in ("engine_states", "flap_angles_rad")}


def nominal_from_actual(actual_vehicle, profile, retained_count):
    """Keep actual mass/geometry/reference; never read actual MAIN thrust limits.

    The raw actual vehicle remains private evidence. In particular, changing its
    three main-engine limits cannot change this reconstructed control model.
    Physical availability lives in State6DOF and is preserved separately.
    """
    _require(type(actual_vehicle) is dyn.Vehicle6DOF and retained_count in RETAINED_COUNTS
        and type(retained_count) is int, "final_sensor_actual_vehicle_required")
    frozen = vehicle(profile, payload_count=retained_count)
    _require(profile["payload"]["count"] == 26
        and len(actual_vehicle.engines) == len(frozen.engines)
        and [e.name for e in actual_vehicle.engines] == [e.name for e in frozen.engines]
        and len(actual_vehicle.aero_panels) == len(frozen.aero_panels)
        and math.isclose(actual_vehicle.dry_mass_kg, frozen.dry_mass_kg,
            rel_tol=1e-12, abs_tol=1e-6), "final_sensor_inventory_or_vehicle_layout_mismatch")
    return replace(actual_vehicle, engines=tuple(
        replace(engine, max_thrust_n=frozen.engines[i].max_thrust_n) if i in obs.MAIN else engine
        for i, engine in enumerate(actual_vehicle.engines)))


def _inventory(value, state_time_s):
    # Origin verification owns separation dynamics. Here the trusted handoff's
    # closed counts, receipt order and complete chain preimage are bound intact.
    keys = {"schema", "total_count", "released_count", "retained_count", "generation",
        "sequencer_state", "pending_action", "observed_time_s", "release_receipts"}
    _require(type(value) is dict and set(value) == keys and value["schema"] == INVENTORY_SCHEMA
        and type(value["total_count"]) is int and value["total_count"] == 26
        and type(value["retained_count"]) is int and value["retained_count"] in RETAINED_COUNTS
        and type(value["released_count"]) is int and type(value["generation"]) is int
        and type(value["release_receipts"]) is list
        and value["released_count"] == value["generation"] == len(value["release_receipts"])
        and value["retained_count"]+value["released_count"] == 26
        and value["pending_action"] is None
        and (value["sequencer_state"] in ("skipped", "inhibited")
            or value["retained_count"] == 0 and value["sequencer_state"] == "running")
        and number(value["observed_time_s"]) and 0 <= value["observed_time_s"] <= state_time_s,
        "final_sensor_closed_actual_inventory_required")
    previous = -1.
    for row in value["release_receipts"]:
        _require(type(row) is dict and number(row.get("time_s"))
            and previous < row["time_s"] <= value["observed_time_s"],
            "final_sensor_release_receipt_clock")
        previous = row["time_s"]


def validate_snapshot(snapshot):
    """Return disconnected state/nominal/history; this is not admission."""
    value = json_copy(snapshot)
    integration = snapshot_integration_fields(value)
    _require(type(value) is dict and set(value) == SNAPSHOT_KEYS | set(integration)
        and value["schema"] == (SNAPSHOT_SCHEMA_V2 if integration else SNAPSHOT_SCHEMA)
        and type(value["run_id"]) is str and _RUN.fullmatch(value["run_id"])
        and is_hash(value["handoff_sha256"]) and is_hash(value["snapshot_id"])
        and value["snapshot_id"] == obs.digest({k: v for k, v in value.items() if k != "snapshot_id"}),
        "invalid_final_sensor_snapshot_identity")
    _require(type(value["state"]) is dict
        and set(value["state"]) == {f.name for f in fields(dyn.State6DOF)}
        and type(value["actual_vehicle"]) is dict
        and set(value["actual_vehicle"]) == {f.name for f in fields(dyn.Vehicle6DOF)},
        "incomplete_final_sensor_rigid_capture")
    state = dyn.state_from_dict(value["state"])
    actual = dyn.vehicle_from_dict(value["actual_vehicle"])
    _inventory(value["inventory"], state.time_s)
    nominal = nominal_from_actual(actual, value["profile"], value["retained_count"])
    history = AvailableCapacityController.from_dict(value["controller"])
    _require(type(value["retained_count"]) is int and type(value["released_count"]) is int
        and value["retained_count"] == value["inventory"]["retained_count"]
        and value["released_count"] == value["inventory"]["released_count"]
        and history.released_count == value["released_count"]
        and history.phase == "ballistic_return" and history.record.get("status") == "active"
        and history.policy == "trimmed_state_terminal_v4" and history.allocation_enabled
        and history.return_time_s <= state.time_s
        and len(state.engine_states) == len(nominal.engines)
        and len(state.flap_angles_rad) == len(nominal.aero_panels)
        and all(abs(engine.gimbal_x_rad) <= spec.max_gimbal_rad+1e-8
            and abs(engine.gimbal_y_rad) <= spec.max_gimbal_rad+1e-8
            for engine, spec in zip(state.engine_states, nominal.engines))
        and all(abs(angle) <= spec.max_deflection_rad+1e-8
            for angle, spec in zip(state.flap_angles_rad, nominal.aero_panels))
        and state.propellant_kg <= nominal.propellant_capacity_kg
        and json_copy(asdict(state)) == value["state"]
        and json_copy(asdict(actual)) == value["actual_vehicle"]
        and history.to_dict() == value["controller"], "unsupported_final_sensor_snapshot_context")
    expected_hashes = {"state_sha256": obs.digest(value["state"]),
        "actual_vehicle_sha256": obs.digest(value["actual_vehicle"]),
        "nominal_vehicle_sha256": obs.digest(asdict(nominal)),
        "controller_sha256": obs.digest(value["controller"]),
        "actuator_sha256": obs.digest(_actuators(value["state"])),
        "profile_sha256": obs.digest(value["profile"]),
        "inventory_sha256": obs.digest(value["inventory"]),
        "inventory_receipt_chain_sha256": obs.digest(value["inventory"]["release_receipts"])}
    _require(all(is_hash(value[key]) and value[key] == identity for key, identity in expected_hashes.items()),
        "final_sensor_snapshot_preimage_hash_mismatch")
    cutoff, complete, application = (value[k] for k in
        ("cutoff_time_s", "complete_time_s", "application_time_s"))
    _require(all(number(x) for x in (cutoff, complete, application))
        and 0 <= cutoff < complete <= state.time_s < application
        and complete >= cutoff+obs.ESTIMATOR["burn_data_window_after_cutoff_s"]-1e-8
        and math.isclose(application-complete, 600., rel_tol=0., abs_tol=1e-8),
        "invalid_final_sensor_snapshot_clock")
    return state, nominal, history


def make_snapshot(state, actual_vehicle, controller, profile, *, run_id, handoff_sha256,
                  inventory, cutoff_time_s, complete_time_s, application_time_s, integration_policy=None):
    """Capture through the trusted execution seam, without resetting history."""
    _require(isinstance(controller, obs.SwitchingController) and controller.switch is None
        and not controller.audit.switches, "final_sensor_requires_unswitched_controller")
    nominal = nominal_from_actual(actual_vehicle, profile, inventory["retained_count"])
    _require(controller.nominal == nominal and controller.model == nominal
        and controller.profile == profile and controller.model is not actual_vehicle,
        "final_sensor_requires_separate_nominal_control_model")
    integration = integration_fields(integration_policy)
    value = json_copy({"schema": SNAPSHOT_SCHEMA_V2 if integration else SNAPSHOT_SCHEMA,
        **integration, "run_id": run_id,
        "handoff_sha256": handoff_sha256, "retained_count": inventory["retained_count"],
        "released_count": inventory["released_count"], "cutoff_time_s": cutoff_time_s,
        "complete_time_s": complete_time_s, "application_time_s": application_time_s,
        "state": asdict(state), "actual_vehicle": asdict(actual_vehicle),
        "controller": controller.controller.to_dict(), "profile": profile, "inventory": inventory})
    value.update(state_sha256=obs.digest(value["state"]),
        actual_vehicle_sha256=obs.digest(value["actual_vehicle"]),
        nominal_vehicle_sha256=obs.digest(asdict(nominal)), controller_sha256=obs.digest(value["controller"]),
        actuator_sha256=obs.digest(_actuators(value["state"])), profile_sha256=obs.digest(value["profile"]),
        inventory_sha256=obs.digest(value["inventory"]),
        inventory_receipt_chain_sha256=obs.digest(value["inventory"]["release_receipts"]))
    value["snapshot_id"] = obs.digest(value)
    validate_snapshot(value)
    return value


def source_inventory(extra_paths=()):
    """Current develop-root source bytes, never a historical source registration."""
    result = {}
    for name in sorted(REQUIRED_SOURCE | set(extra_paths)):
        _require(type(name) is str and not Path(name).is_absolute() and ".." not in Path(name).parts,
            "final_sensor_source_path_unsafe")
        path = (ROOT/name).resolve()
        _require(path.is_relative_to(ROOT) and path.is_file(), "final_sensor_source_path_missing")
        result[name] = sha256(path.read_bytes()).hexdigest()
    return result


def current_source_checks(source_sha256, expected_backend):
    """Bind installed numerics and loaded local modules to this develop root."""
    checks = {"source_inventory_complete": False, "current_source_bytes_match": False,
        "loaded_modules_from_current_root": False, "installed_backend_matches": backend() == expected_backend}
    try:
        _require(type(source_sha256) is dict and REQUIRED_SOURCE <= source_sha256.keys()
            and all(is_hash(identity) for identity in source_sha256.values()),
            "final_sensor_source_inventory_incomplete")
        actual = source_inventory(source_sha256)
        checks["source_inventory_complete"] = True
        checks["current_source_bytes_match"] = actual == source_sha256
        checks["loaded_modules_from_current_root"] = all(
            Path(module.__file__).resolve() == (ROOT/name).resolve()
            for name in REQUIRED_SOURCE
            for module in [sys.modules.get(name[:-3].replace("/", "."))] if module is not None)
    except (OSError, ValueError, TypeError):
        pass
    return checks


def _estimate(value, snapshot_time):
    _require(type(value) is dict and is_hash(value.get("estimate_id"))
        and value["estimate_id"] == obs.digest({k: v for k, v in value.items() if k != "estimate_id"})
        and value.get("observed_at_s") == snapshot_time
        and number(value.get("factor_estimate")) and type(value.get("band")) is list
        and len(value["band"]) == 2 and all(number(x) for x in value["band"])
        and obs.FACTOR_RANGE[0] <= value["band"][0] < value["factor_estimate"]
            < value["band"][1] <= obs.FACTOR_RANGE[1], "final_sensor_invalid_observation_estimate")
    return dict(zip(POINT_LABELS, (value["band"][0], value["factor_estimate"], value["band"][1])))


def _record_checks(record, snapshot):
    """Recheck exact capture plus ordered numerical model/plant boundaries."""
    state, nominal, _ = validate_snapshot(snapshot)
    limits = [e.max_thrust_n for e in nominal.engines]
    rows = record["control_capability_audit"]["all_boundary_calls"]
    ordered, position, steps = True, 0, 0
    while position < len(rows):
        block = rows[position:position+2]
        if [row["boundary"] for row in block] != ["update", "capability_profile"]:
            ordered = False
            break
        position += 2
        if position < len(rows) and rows[position]["boundary"] == "command":
            block += rows[position:position+4]
            if [row["boundary"] for row in block[2:]] != ["command", "capability_profile", "tvc_control", "plant"]:
                ordered = False
                break
            position += 4
            steps += 1
        ordered &= all(row["time_s"] == block[0]["time_s"] for row in block)
    captured = snapshot["state"]
    checks = {"same_snapshot": record["prediction_snapshot_id"] == snapshot["snapshot_id"],
        "same_current_state": record["initial_state"] == captured,
        "same_nominal_limits": record["nominal_engine_limits_n"] == limits,
        "same_controller_history": record["prediction_controller_sha256"] == snapshot["controller_sha256"],
        "same_inventory": record["prediction_inventory_sha256"] == snapshot["inventory_sha256"]
            and record["prediction_retained_count"] == snapshot["retained_count"]
            and record["prediction_released_count"] == snapshot["released_count"],
        "same_nominal_vehicle": record["prediction_nominal_vehicle_sha256"] == snapshot["nominal_vehicle_sha256"],
        "same_actuators": record["prediction_actuator_sha256"] == snapshot["actuator_sha256"],
        "no_execution_or_qualification": record["prediction_is_execution"] is False
            and record["runtime_admission"] is False and record["fault_domain_status"] == "unverified"
            and record["online_prediction_scope"] == SCOPE,
        "ordered_model_to_hypothetical_plant": bool(rows) and ordered
            and steps == record["outcome"]["integration_steps"]
            and rows[0]["time_s"] == state.time_s,
        "captured_unavailable_engines_not_resurrected": all(
            sample["engine_states"][i]["available"] is False
            for sample in record["samples"] for i, engine in enumerate(captured["engine_states"])
            if engine["available"] is False)}
    return {"passed": bool(checks) and all(checks.values()), "checks": checks}


def forecast_job(job):
    """One bounded numerical branch, with no access to the execution plant."""
    value = json_copy(job)
    _require(type(value) is dict and "snapshot" in value, "invalid_final_sensor_forecast_job")
    snapshot = value["snapshot"]
    state, nominal, history = validate_snapshot(snapshot)
    integration = snapshot_integration_fields(snapshot)
    _require(set(value) == JOB_KEYS | set(integration)
        and all(value[key] == item for key, item in integration.items()),
        "final_sensor_forecast_job_integration_policy_mismatch")
    policy = policy_for_snapshot(snapshot)
    sampled_points = _estimate(value["sampling_estimate"], state.time_s)
    _estimate(value["accelerometer_estimate"], state.time_s)
    action, sampled, label = value["action"], value["sampled_factor"], value["sample_label"]
    _require(action in ACTIONS and label in POINT_LABELS and number(sampled)
        and sampled == sampled_points[label]
        and number(value["timeout_s"]) and 0 < value["timeout_s"] <= 180.,
        "unsupported_final_sensor_forecast_job")
    _require(all(current_source_checks(value["source_sha256"], value["backend"]).values()),
        "final_sensor_source_or_backend_changed")
    model = obs.model_vehicle(nominal, 1.)
    plant = obs.model_vehicle(nominal, sampled)
    audit = obs.SwitchAudit(nominal, model, plant)
    controller = obs.SwitchingController(nominal, model, snapshot["profile"],
        history.return_time_s, history.released_count, audit)
    controller.controller = history
    controller.rebind_audit(plant)
    application = snapshot["application_time_s"]
    estimate = value["accelerometer_estimate"]
    started = time.monotonic()

    def apply(s, c, events):
        if action == ADOPT and c.switch is None and s.time_s >= application-1e-9:
            _require(math.isclose(s.time_s, application, rel_tol=0., abs_tol=1e-8),
                "final_sensor_prediction_missed_application_step")
            c.apply_switch(s.time_s, estimate["factor_estimate"], {
                "kind": "final_sensor_numeric_estimate", "estimate_id": estimate["estimate_id"],
                "snapshot_id": snapshot["snapshot_id"]})
        return True

    # This numerical branch owns its hook. A task wrapper must not install a
    # hardcoded or nested policy around it. No execution plant is reachable.
    with restored_integration(policy) as execution:
        run = obs.propagate(state, plant, controller, snapshot["profile"],
            end_time_s=state.time_s+3600., deadline=started+value["timeout_s"], before_update=apply,
            step_boundary_s=application)
    integration_execution = execution.record()
    if integration:
        # A caught numerical failure remains a saved negative with its full
        # audit. Completeness/preview/guard admission below still requires a
        # completed receipt; do not discard failed evidence at construction.
        validate_execution_receipt(integration_execution, policy, require_completed=False)
    record = json_copy(obs.record("final_sensor_counterfactual", snapshot["state"], plant,
        nominal, controller, run, plant_factor=sampled,
        switch=None if controller.switch is None else {
            "factor": controller.switch["factor"], "time_s": controller.switch["time_s"]},
        wall_time_s=time.monotonic()-started, extra={
            "prediction_snapshot_id": snapshot["snapshot_id"],
            "prediction_controller_sha256": snapshot["controller_sha256"],
            "prediction_nominal_vehicle_sha256": snapshot["nominal_vehicle_sha256"],
            "prediction_inventory_sha256": snapshot["inventory_sha256"],
            "prediction_actuator_sha256": snapshot["actuator_sha256"],
            "prediction_retained_count": snapshot["retained_count"],
            "prediction_released_count": snapshot["released_count"],
            "prediction_is_execution": False, "online_prediction_scope": SCOPE}))
    result = {"schema": FORECAST_SCHEMA_V2 if integration else FORECAST_SCHEMA,
        **integration, **({"integration_execution": integration_execution} if integration else {}),
        "snapshot_id": snapshot["snapshot_id"], "action": action,
        "sample_label": label, "sampled_factor": sampled,
        "assumed_factor": estimate["factor_estimate"] if action == ADOPT else 1.,
        "application_time_s": application, "accelerometer_estimate_id": estimate["estimate_id"],
        "sampling_estimate_id": value["sampling_estimate"]["estimate_id"],
        "source_sha256": value["source_sha256"], "backend": value["backend"], "record": record,
        "verification": obs.verify_record(record, CONTACT_LIMITS),
        "stronger_record_checks": _record_checks(record, snapshot)}
    result["forecast_id"] = obs.digest(result)
    _require(obs.encoded(job) == obs.encoded(value), "final_sensor_forecast_mutated_job")
    return result


def ordered_forecasts(forecasts):
    _require(type(forecasts) is list and len(forecasts) == 6, "six_final_sensor_forecasts_required")
    ordered = []
    binding = None
    for action in ACTIONS:
        rows = [row for row in forecasts if type(row) is dict and row.get("action") == action]
        _require(len(rows) == 3 and {row.get("sample_label") for row in rows} == set(POINT_LABELS),
            "final_sensor_forecast_grid_incomplete")
        rows.sort(key=lambda row: POINT_LABELS.index(row["sample_label"]))
        for row in rows:
            _forecast_fields(row)
            _require(is_hash(row["forecast_id"])
                and row["forecast_id"] == obs.digest({k: v for k, v in row.items() if k != "forecast_id"}),
                "final_sensor_forecast_identity_mismatch")
        points = [row["sampled_factor"] for row in rows]
        _require(all(number(x) for x in points) and points[0] < points[1] < points[2],
            "final_sensor_forecast_grid_mismatch")
        for row in rows:
            context = {key: row[key] for key in ("schema", "snapshot_id", "application_time_s",
                "accelerometer_estimate_id", "sampling_estimate_id", "source_sha256", "backend")}
            if row["schema"] == FORECAST_SCHEMA_V2:
                context["integration_policy"] = row["integration_policy"]
            if binding is None:
                binding = (context, points)
            _require(binding == (context, points), "final_sensor_forecast_capture_mismatch")
        ordered.extend(rows)
    return ordered


def validate_forecast(result, snapshot):
    """Recompute capture/record predicates without propagating or approving."""
    row = json_copy(result)
    explicit = _forecast_fields(row)
    integration = snapshot_integration_fields(snapshot)
    _require(explicit is bool(integration)
        and all(row[key] == item for key, item in integration.items()),
        "final_sensor_forecast_snapshot_integration_policy_mismatch")
    _require(is_hash(row["forecast_id"])
        and row["forecast_id"] == obs.digest({k: v for k, v in row.items() if k != "forecast_id"})
        and row["snapshot_id"] == snapshot["snapshot_id"]
        and row["application_time_s"] == snapshot["application_time_s"]
        and row["action"] in ACTIONS and row["sample_label"] in POINT_LABELS
        and number(row["sampled_factor"]) and number(row["assumed_factor"])
        and obs.FACTOR_RANGE[0] <= row["sampled_factor"] <= obs.FACTOR_RANGE[1]
        and obs.FACTOR_RANGE[0] <= row["assumed_factor"] <= obs.FACTOR_RANGE[1]
        and is_hash(row["accelerometer_estimate_id"]) and is_hash(row["sampling_estimate_id"]),
        "final_sensor_forecast_binding_mismatch")
    _require(all(current_source_checks(row["source_sha256"], row["backend"]).values()),
        "final_sensor_source_or_backend_changed")
    record = row["record"]
    _require(record["plant_factor"] == row["sampled_factor"], "final_sensor_hypothesis_changed")
    if row["action"] == CONTINUE:
        _require(row["assumed_factor"] == 1. and record["switch"] is None,
            "final_sensor_continue_switch_mismatch")
    else:
        _require(record["switch"] == {"factor": row["assumed_factor"],
            "time_s": snapshot["application_time_s"]}, "final_sensor_adopt_switch_mismatch")
        switches = record["control_capability_audit"]["switches"]
        _require(len(switches) == 1 and switches[0]["basis"] == {
            "kind": "final_sensor_numeric_estimate", "estimate_id": row["accelerometer_estimate_id"],
            "snapshot_id": snapshot["snapshot_id"]}, "final_sensor_adopt_basis_mismatch")
    verified, strong = obs.verify_record(record, CONTACT_LIMITS), _record_checks(record, snapshot)
    _require(verified == row["verification"] and strong == row["stronger_record_checks"],
        "final_sensor_saved_verification_changed")
    return verified, strong


def forecast_preview(forecasts, snapshot):
    """Provider-safe whitelist; excludes vehicle/controller/inventory preimages.

    Opaque snapshot IDs bind the private forecast; numerical output contains only
    contact metrics, a named observation sample and the contact-limit result.
    It does not expose a hidden physical capability or claim full-band validity.
    """
    rows = ordered_forecasts(forecasts)
    result = {"status": "complete", "snapshot_id": rows[0]["snapshot_id"], "scope": SCOPE,
        "actions": {action: [] for action in ACTIONS}}
    for row in rows:
        verified, strong = validate_forecast(row, snapshot)
        metrics = verified["contact"]
        _require(verified["record_valid"] and strong["passed"] is True
            and type(metrics) is dict and all(number(metrics.get(key)) for key in CONTACT_LIMITS),
            "incomplete_final_sensor_forecast")
        result["actions"][row["action"]].append({"sample_label": row["sample_label"],
            "sampled_factor": row["sampled_factor"],
            **{key: metrics[key] for key in CONTACT_LIMITS}, "limits_met": verified["contact_limits_met"]})
    return result


def forecast_evidence(forecasts, snapshot):
    preview = forecast_preview(forecasts, snapshot)
    return {f"{action}.{label}.{key}": value
        for action in ACTIONS for label, point in zip(POINT_LABELS, preview["actions"][action])
        for key, value in point.items() if key != "sample_label"}

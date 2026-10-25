"""Bounded, same-model counterfactuals from a captured return-controller state.

The caller owns execution authority and the process wall deadline. A forecast
rebuilds its hypothetical plant from an observation-derived sample, never from
the execution plant. Prediction records are not flight or qualification records.
Use processes for parallel jobs: the separated controller audits module-level
control boundaries and must not be run concurrently in threads.
"""
from dataclasses import asdict, fields
import json
import math
import re
import time

from . import starship_ship_capability_guard as prior
from . import starship_ship_capability_observation as obs
from . import starship_sixdof as dyn
from .starship_ship_fault_screen import AvailableCapacityController
from .starship_sixdof_mission import vehicle

SNAPSHOT_SCHEMA = "missionos.ship_sensor_online_snapshot.v1"
FORECAST_SCHEMA = "missionos.ship_sensor_online_forecast.v1"
SCOPE = "online_discrete_predictions_same_model_not_qualification"
CONTINUE = "continue_nominal_assumption"
ADOPT = "adopt_accelerometer_estimate"
ACTIONS = (CONTINUE, ADOPT)
POINT_LABELS = ("band_low", "point_estimate", "band_high")
CONTACT_LIMITS = dict(prior.CONTACT_LIMITS)
SNAPSHOT_KEYS = {"schema", "run_id", "handoff_sha256", "retained_count", "cutoff_time_s",
                 "complete_time_s", "application_time_s", "state", "controller", "profile", "snapshot_id"}
JOB_KEYS = {"snapshot", "action", "sampled_factor", "accelerometer_estimate", "source_sha256",
            "backend", "timeout_s"}
FORECAST_KEYS = {"schema", "snapshot_id", "action", "sampled_factor", "assumed_factor",
                 "application_time_s", "source_sha256", "backend", "record", "verification",
                 "stronger_record_checks", "forecast_id"}
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def is_hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def json_copy(value):
    """Require finite JSON and disconnect all mutable caller-owned material."""
    return json.loads(obs.encoded(value))


def validate_snapshot(snapshot):
    """Restore complete history without resetting actuators or granting authority."""
    value = json_copy(snapshot)
    if (type(value) is not dict or set(value) != SNAPSHOT_KEYS
            or value["schema"] != SNAPSHOT_SCHEMA
            or not isinstance(value["run_id"], str) or not _RUN.fullmatch(value["run_id"])
            or not is_hash(value["handoff_sha256"])
            or type(value["retained_count"]) is not int or value["retained_count"] != 0
            or not is_hash(value["snapshot_id"])
            or value["snapshot_id"] != obs.digest({k: v for k, v in value.items() if k != "snapshot_id"})):
        raise ValueError("invalid_online_snapshot_identity")
    state_value = value["state"]
    if type(state_value) is not dict or set(state_value) != {f.name for f in fields(dyn.State6DOF)}:
        raise ValueError("incomplete_online_snapshot_state")
    state = dyn.state_from_dict(state_value)
    nominal = vehicle(value["profile"], payload_count=0)
    controller = AvailableCapacityController.from_dict(value["controller"])
    if (controller.phase != "ballistic_return" or controller.record.get("status") != "active"
            or controller.policy != "trimmed_state_terminal_v4" or not controller.allocation_enabled
            or controller.released_count != value["profile"]["payload"]["count"]
            or controller.terminal_engine_out or controller.failed_engine
            or controller.return_time_s > state.time_s
            or len(state.engine_states) != len(nominal.engines)
            or len(state.flap_angles_rad) != len(nominal.aero_panels)
            or not all(state.engine_states[i].available for i in obs.MAIN)
            or abs(sum(x*x for x in state.q_body_to_eci)-1.) > 1e-8
            or json_copy(asdict(state)) != state_value):
        raise ValueError("unsupported_online_snapshot_context")
    cutoff, complete, application = (value[k] for k in
        ("cutoff_time_s", "complete_time_s", "application_time_s"))
    if (not all(number(x) for x in (cutoff, complete, application))
            or not 0 <= cutoff < complete <= state.time_s < application
            or complete < cutoff+obs.ESTIMATOR["burn_data_window_after_cutoff_s"]-1e-8
            or not math.isclose(application-complete, 600., rel_tol=0., abs_tol=1e-8)):
        raise ValueError("invalid_online_snapshot_clock")
    return state, nominal, controller


def make_snapshot(state, controller, profile, *, run_id, handoff_sha256, retained_count,
                  cutoff_time_s, complete_time_s, application_time_s):
    """Capture an unswitched, nominal-assumption controller through a trusted seam."""
    if not isinstance(controller, obs.SwitchingController) or controller.switch is not None:
        raise ValueError("online_snapshot_requires_unswitched_controller")
    nominal = vehicle(profile, payload_count=retained_count)
    if (controller.nominal != nominal or controller.model != nominal
            or controller.profile != profile or controller.audit.switches):
        raise ValueError("online_snapshot_requires_nominal_control_model")
    result = json_copy({"schema": SNAPSHOT_SCHEMA, "run_id": run_id,
        "handoff_sha256": handoff_sha256, "retained_count": retained_count,
        "cutoff_time_s": cutoff_time_s, "complete_time_s": complete_time_s,
        "application_time_s": application_time_s, "state": asdict(state),
        "controller": controller.controller.to_dict(), "profile": profile})
    result["snapshot_id"] = obs.digest(result)
    validate_snapshot(result)
    return result


def forecast_job(job):
    """Run one finite hypothesis; numerical/time failures remain in its record.

    ``timeout_s`` is a cooperative integration bound. The caller must also bound
    the process lifetime, including a long individual integration/control step.
    """
    value = json_copy(job)
    if type(value) is not dict or set(value) != JOB_KEYS:
        raise ValueError("invalid_online_forecast_job")
    snapshot = value["snapshot"]
    state, nominal, history = validate_snapshot(snapshot)
    action, sampled, current = value["action"], value["sampled_factor"], value["accelerometer_estimate"]
    if (action not in ACTIONS or not number(sampled)
            or not obs.FACTOR_RANGE[0] <= sampled <= obs.FACTOR_RANGE[1]
            or not number(value["timeout_s"]) or not 0 < value["timeout_s"] <= 180.
            or type(current) is not dict or not is_hash(current.get("estimate_id"))
            or current["estimate_id"] != obs.digest({k: v for k, v in current.items() if k != "estimate_id"})
            or not number(current.get("factor_estimate"))
            or not obs.FACTOR_RANGE[0] <= current["factor_estimate"] <= obs.FACTOR_RANGE[1]
            or current.get("observed_at_s") != state.time_s
            or type(value["source_sha256"]) is not dict or not value["source_sha256"]
            or not all(isinstance(k, str) and is_hash(v) for k, v in value["source_sha256"].items())
            or type(value["backend"]) is not dict):
        raise ValueError("unsupported_online_forecast_job")
    if not all(prior.current_source_checks(value["source_sha256"], value["backend"]).values()):
        raise ValueError("online_forecast_source_or_backend_changed")
    # Reconstruct every model from the frozen nominal profile. No execution
    # plant, physical factor, fault case or prior model audit is accepted.
    model = obs.model_vehicle(nominal, 1.)
    plant = obs.model_vehicle(nominal, sampled)
    audit = obs.SwitchAudit(nominal, model, plant)
    controller = obs.SwitchingController(nominal, model, snapshot["profile"],
        history.return_time_s, history.released_count, audit)
    controller.controller = history
    application = snapshot["application_time_s"]
    started = time.monotonic()

    def apply(s, c, events):
        if action == ADOPT and c.switch is None and s.time_s >= application-1e-9:
            if not math.isclose(s.time_s, application, rel_tol=0., abs_tol=1e-8):
                raise ValueError("online_prediction_missed_application_step")
            c.apply_switch(s.time_s, current["factor_estimate"],
                {"kind": "online_numeric_estimate", "estimate_id": current["estimate_id"],
                 "snapshot_id": snapshot["snapshot_id"]})
        return True

    run = obs.propagate(state, plant, controller, snapshot["profile"],
        end_time_s=state.time_s+3600., deadline=started+value["timeout_s"], before_update=apply,
        step_boundary_s=application)
    record = json_copy(obs.record("online_sensor_counterfactual", snapshot["state"], plant,
        nominal, controller, run, plant_factor=sampled,
        switch=None if controller.switch is None else {
            "factor": controller.switch["factor"], "time_s": controller.switch["time_s"]},
        wall_time_s=time.monotonic()-started,
        extra={"prediction_snapshot_id": snapshot["snapshot_id"],
               "prediction_controller_sha256": obs.digest(snapshot["controller"]),
               "prediction_is_execution": False, "online_prediction_scope": SCOPE}))
    result = {"schema": FORECAST_SCHEMA, "snapshot_id": snapshot["snapshot_id"], "action": action,
        "sampled_factor": sampled, "assumed_factor": current["factor_estimate"] if action == ADOPT else 1.,
        "application_time_s": application, "source_sha256": value["source_sha256"], "backend": value["backend"],
        "record": record, "verification": obs.verify_record(record, CONTACT_LIMITS),
        "stronger_record_checks": prior.stronger_record_checks(record, snapshot["profile"], 0)}
    result["forecast_id"] = obs.digest(result)
    if obs.encoded(job) != obs.encoded(value):
        # ``value`` is also the immutable local source. This is intentionally
        # checked after the predictor has used restored copies of its contents.
        raise AssertionError("online_forecast_mutated_job")
    return result


def ordered_forecasts(forecasts):
    if type(forecasts) is not list or len(forecasts) != 6:
        raise ValueError("exactly_six_online_forecasts_required")
    ordered = []
    grid = None
    for action in ACTIONS:
        rows = [row for row in forecasts if isinstance(row, dict) and row.get("action") == action]
        if len(rows) != 3 or any(not number(row.get("sampled_factor")) for row in rows):
            raise ValueError("online_forecast_action_grid_incomplete")
        rows.sort(key=lambda row: row["sampled_factor"])
        points = [row["sampled_factor"] for row in rows]
        if not points[0] < points[1] < points[2] or grid is not None and points != grid:
            raise ValueError("online_forecast_action_grid_mismatch")
        grid = points
        ordered.extend(rows)
    return ordered


def forecast_preview(forecasts):
    """Expose only recomputed contact summaries, never raw forecast/plant records."""
    rows = ordered_forecasts(forecasts)
    ids = {row["snapshot_id"] for row in rows}
    if len(ids) != 1:
        raise ValueError("online_forecast_snapshot_mismatch")
    result = {"status": "complete", "snapshot_id": rows[0]["snapshot_id"], "scope": SCOPE,
              "actions": {action: [] for action in ACTIONS}}
    for row in rows:
        verified = obs.verify_record(row["record"], CONTACT_LIMITS)
        metrics = verified["contact"]
        if metrics is None or any(not number(metrics.get(key)) for key in CONTACT_LIMITS):
            raise ValueError("incomplete_online_forecasts")
        result["actions"][row["action"]].append({"sampled_factor": row["sampled_factor"],
            **{key: metrics[key] for key in CONTACT_LIMITS}, "limits_met": verified["contact_limits_met"]})
    return result


def forecast_evidence(forecasts):
    preview = forecast_preview(forecasts)
    return {f"{action}.{label}.{key}": value
        for action in ACTIONS for label, point in zip(POINT_LABELS, preview["actions"][action])
        for key, value in point.items()}

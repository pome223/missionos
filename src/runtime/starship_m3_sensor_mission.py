"""Opt-in M3 composition with a same-flight Ship symptom controller.

The canonical ascent/deployment prefix keeps its own verifier. A new return
experiment cannot borrow the nominal return verdict or a saved flight's state.
Importing this module does not launch a plant, forecast, or provider request.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time

from .starship_artifacts import write_verified_checkpoint, write_verified_input
from .starship_replanning import ROOT, digest, source_hashes as return_sources

SCHEMA = "missionos.starship_m3_sensor_mission.v1"
REGISTRATION_SCHEMA = "missionos.starship_m3_sensor_registration.v1"
REGISTRATION = "examples/spaceflight/starship-m3-sensor-registration-v2.json"
CASES = {
    "normal": {"physical_factor": 1.0, "accelerometer_gain": 1.0},
    "propulsion_80": {"physical_factor": 0.8, "accelerometer_gain": 1.0},
    "sensor_gain_80": {"physical_factor": 1.0, "accelerometer_gain": 0.8},
}
OWN_SOURCES = (
    "src/runtime/starship_m3_sensor_mission.py",
    "scripts/run_starship_m3_sensor.py",
)


def json_value(value):
    return json.loads(json.dumps(value, allow_nan=False))


def validate_registration(value):
    """Accept one explicitly bounded new cohort; never consume old flight data."""
    if (type(value) is not dict or value.get("schema") != REGISTRATION_SCHEMA
            or value.get("base_case") != "orbital_update"
            or value.get("maximum_attempts_per_case") != 1
            or value.get("cases") != [{"case_id": key, **row} for key, row in CASES.items()]
            or type(value.get("sensor_config")) is not dict
            or type(value.get("source_files")) is not list):
        raise ValueError("m3_sensor_registration_not_supported")
    limits = {"maximum_wall_time_s": 1800, "maximum_output_bytes": 1024**3,
              "minimum_free_bytes": 5*1024**3}
    if any(type(value.get(k)) not in (int, float) or value[k] != v for k, v in limits.items()):
        raise ValueError("m3_sensor_registered_budget_mismatch")
    for name in value["source_files"]:
        path = Path(name)
        if (type(name) is not str or path.is_absolute() or ".." in path.parts
                or path.suffix not in (".py", ".json", ".md")
                or not (ROOT / path).is_file() or (ROOT / path).is_symlink()):
            raise ValueError("m3_sensor_source_path_invalid")
    return deepcopy(value)


def source_hashes(registration_file=REGISTRATION):
    from .starship_m3_live import sources
    path = Path(registration_file)
    if path.is_absolute() or ".." in path.parts or (ROOT/path).is_symlink():
        raise ValueError("m3_sensor_registration_path_invalid")
    registration = validate_registration(json.loads((ROOT/path).read_bytes()))
    names = {*sources(), *OWN_SOURCES, str(path), *registration["source_files"]}
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(names)}


def run(profile, case, output, *, mode, authority, mailbox, run_id,
        sensor_config, decide, forecast_map, capture, guard_check, deadline,
        source_sha256=None):
    """Run a new prefix and its exact return continuation once, saving failures."""
    from . import starship_m3_live as m3
    from .starship_m3_sensor_executor import SymptomReplanningExecutor

    if (mode not in ("fixture", "live") or case not in CASES
            or authority != m3.contract(mode, "orbital_update")
            or any(sensor_config.get(k) != v for k, v in CASES[case].items())):
        raise ValueError("m3_sensor_mission_authority_or_case")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("m3_sensor_fresh_output_required")
    output.mkdir(parents=True, mode=0o700)
    frozen = deepcopy(source_sha256 if source_sha256 is not None else source_hashes())
    inner = authority["m2_authority"]
    started, actor = time.monotonic(), None
    study = {"schema": SCHEMA, "run_id": run_id, "case_id": case, "mode": mode,
             "authority": deepcopy(authority), "profile": deepcopy(profile),
             "sensor_config": deepcopy(sensor_config), "source_sha256": frozen,
             "return_source_sha256": return_sources(), "prefix": None, "return_study": None,
             "runtime_failure": None, "retry_performed": False, "physical_execution": False,
             "mission_completed": False, "m3_completed": False}
    write_verified_input(output/"inputs.json", {k: v for k, v in study.items()
                         if k not in ("prefix", "return_study")})
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("m3_sensor_deadline_before_prefix")
        m3.validate_worker(authority, mode=mode, case="orbital_update", run_id=run_id, mailbox=mailbox)
        prefix = m3.produce_prefix(profile, inner, output/"prefix",
            expected_prefix_authority=authority["prefix_authority"], run_id=run_id, mailbox=mailbox)
        study["prefix"] = prefix
        m3.validate_prefix(prefix, inner, profile,
            expected_prefix_authority=authority["prefix_authority"], expected_run_id=run_id)
        if time.monotonic() >= deadline:
            raise TimeoutError("m3_sensor_deadline_before_return")
        returning = output/"return"
        returning.mkdir(mode=0o700)
        actor = SymptomReplanningExecutor(profile, inner["return_authority"], inner["return_case"],
            returning, run_id=run_id+"-return", mailbox=Path(mailbox)/"return" if mode == "live" else None,
            deployment={"authority": inner, "handoff": prefix["handoff"], "launch": prefix["prefix_run"]},
            sensor_config=deepcopy(sensor_config), decide=decide, forecast_map=forecast_map,
            capture=capture, guard_check=guard_check, deadline=deadline, mode=mode)
        study["return_study"] = actor.run()
    except Exception as error:
        study["runtime_failure"] = {"type": type(error).__name__, "message": str(error),
                                    "retry_performed": False}
        write_verified_input(output/"failure.json", study["runtime_failure"])
        if actor is not None:
            try:
                study["return_study"] = actor.finish()
            except Exception as finish_error:
                study["finish_failure"] = {"type": type(finish_error).__name__, "message": str(finish_error)}
    finally:
        study["wall_time_s"] = time.monotonic()-started
        study["sources_unchanged"] = all((ROOT/name).is_file()
            and sha256((ROOT/name).read_bytes()).hexdigest() == expected for name, expected in frozen.items())
        study = write_verified_checkpoint(output/"study.json", json_value(study))
    return study


def verify(study, *, expected_authority, expected_sources, expected_run_id,
           expected_sensor_config, authoritative_capture=None, sensor_verifier=None):
    """Keep historical nominal checks and new sensor checks explicitly separate."""
    from .starship_m3_prefix_verifier import verify as verify_prefix
    from .starship_m2_contract import validate_deployment_origin
    from .starship_replanning_verifier import verify as verify_return

    result = {"schema": "missionos.starship_m3_sensor_verification.v1", "passed": False,
              "issues": [], "prefix_verified": False, "same_flight_verified": False,
              "nominal_return_verified": False, "return_planning_verified": False, "sensor_verified": False,
              "ai_control_completed": False, "mission_completed": False,
              "m3_completed": False, "physical_execution": False,
              "scope": "same_flight_experimental_sensor_control_and_recorded_contact",
              "nominal_qualification_extended": False}
    try:
        if (study["schema"] != SCHEMA or study["authority"] != expected_authority
                or study["source_sha256"] != expected_sources or study["run_id"] != expected_run_id
                or study["sensor_config"] != expected_sensor_config
                or study["case_id"] not in CASES
                or any(expected_sensor_config.get(k) != value for k, value in CASES[study["case_id"]].items())
                or study["return_source_sha256"] != return_sources()
                or any(expected_sources.get(k) != v for k, v in study["return_source_sha256"].items())
                or study["sources_unchanged"] is not True or study["runtime_failure"] is not None
                or study["retry_performed"] is not False or study["physical_execution"] is not False):
            raise ValueError("m3_sensor_approved_runtime_binding")
        prefix, returning = study["prefix"], study["return_study"]
        inner = expected_authority["m2_authority"]
        prefix_check = verify_prefix(prefix)
        result["prefix_verification"] = prefix_check
        result["prefix_verified"] = bool(prefix_check.get("passed") and prefix_check.get("handoff_ready"))
        context = validate_deployment_origin(study["profile"], inner, prefix["handoff"], prefix["prefix_run"])
        expected_binding = {k: context[k] for k in ("handoff_sha256", "authority_sha256", "profile_sha256",
                                                   "generation", "released_count", "retained_count")}
        result["same_flight_verified"] = (
            prefix["run_id"] == expected_run_id and prefix["profile"] == study["profile"]
            and prefix["authority"] == expected_authority["prefix_authority"]
            and returning["run_id"] == expected_run_id+"-return"
            and json_value(returning["execution_origin"]) == prefix["handoff"]["execution_state"]
            and returning["launch"] == prefix["prefix_run"]
            and returning["deployment_binding"] == expected_binding)
        # This result is never changed to pass because sensor execution succeeded.
        old = verify_return(returning, expected_envelope=inner["return_authority"],
            expected_case=inner["return_case"], expected_sources=study["return_source_sha256"],
            expected_run_id=expected_run_id+"-return", expected_deployment_binding=expected_binding)
        result["nominal_return_verification"] = old
        result["nominal_return_verified"] = old.get("passed") is True
        planning = verify_planning(returning, inner["return_authority"], inner["return_case"],
                                   study["return_source_sha256"], expected_binding, expected_run_id+"-return")
        result["return_planning_verification"] = planning
        result["return_planning_verified"] = planning["passed"]
        checked = (sensor_verifier or verify_sensor)(returning,
            authoritative_capture=authoritative_capture, expected_config=expected_sensor_config,
            expected_mode=expected_authority["mode"])
        result["sensor_verification"] = checked
        result["sensor_verified"] = checked.get("passed") is True
        result["passed"] = all(result[k] for k in (
            "prefix_verified", "same_flight_verified", "return_planning_verified", "sensor_verified"))
        if study["case_id"] == "normal":
            result["passed"] &= result["nominal_return_verified"]
        result["ai_control_completed"] = result["passed"] and checked.get("ai_control_completed") is True
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        result["issues"].append(str(error))
    return result


def verify_planning(study, envelope, case, sources, deployment_binding, run_id):
    """Recompute orbital plan and dispatch admission, without fault-contact claims."""
    from .starship_replanning_verifier import (
        admit, decision_budget_reasons, planning_window_reasons, deadline_fallback_reasons,
    )
    checks, errors = {}, []
    try:
        checks["approved_planning_context"] = (study["envelope"] == envelope and study["case"] == case
            and study["run_id"] == run_id and study["source_sha256"] == sources
            and study["deployment_binding"] == deployment_binding)
        candidates, revisions = study["candidates"], study["plan_revisions"]
        checks["forecast_sources_and_observed_origins"] = bool(candidates) and all(
            value["source_sha256"] == sources and all(
                trial["origin"]["basis"] == "synthetic_navigation_estimate_v1" for trial in value["trials"])
            for value in candidates.values())
        checks["planner_budgets"] = (0 < len(revisions) <= envelope["maximum_plan_revisions"]
            and study["forecast_count"] <= envelope["maximum_forecasts"])
        decisions = study["decisions"]
        by_id = {row["request"]["request_id"]: row for row in decisions}
        checks["decision_identities"] = len(by_id) == len(decisions) and all(
            row["request"]["envelope_sha256"] == digest(envelope)
            and row["request"]["request_id"].startswith(run_id+"-") for row in decisions)
        decision_errors = decision_budget_reasons(decisions, envelope["maximum_jev_calls"], envelope=envelope)
        for row in decisions:
            request, response = row["request"], row["response"]
            decision_errors.extend(planning_window_reasons(row, envelope,
                study["profile"]["integration"]["coast_dt_s"] if "orbital_planning_window" in envelope else None))
            decision_errors.extend(deadline_fallback_reasons(row, study, envelope,
                expected_deployment_binding=deployment_binding))
            if response is not None and (response.get("request_sha256") != digest(request)
                    or response.get("request_id") != request["request_id"]):
                decision_errors.append("response_binding")
            if row["accepted_action"] is not None and (response is None
                    or row["accepted_action"] not in request["allowed_actions"]
                    or row["accepted_action"] != response.get("action")
                    or row["later_time_s"] > request["deadline_s"]):
                decision_errors.append("unapproved_decision")
            if (row["later_observation"]["state"]["time_s"] != row["later_time_s"]
                    or row["later_time_s"] < request["time_s"]):
                decision_errors.append("decision_observation_time")
        checks["bounded_decisions"] = not decision_errors
        errors.extend(decision_errors)
        checked_revisions = []
        for index, row in enumerate(revisions):
            candidate, decision = row["candidate_snapshot"], by_id.get(row["decision_request_id"])
            check = admit(candidate, envelope, row["observation"], row["notice"], study["scheduled_s"],
                          expected_deployment_binding=deployment_binding)
            accepted = (row["revision"] == index+1 and row["candidate_sha256"] == digest(candidate)
                and row["check"] == check and check["accepted"] and decision is not None)
            if accepted:
                accepted = (decision["accepted_action"] is None if row["basis"] == "checked_fallback"
                            else decision["accepted_action"] == "select_"+row["candidate_id"])
                if row["basis"] == "ai":
                    accepted &= (decision["response"] or {}).get("model_inference_invoked") is True
            checked_revisions.append(bool(accepted))
        checks["recomputed_plan_admissions"] = bool(checked_revisions) and all(checked_revisions)
        dispatch = study["dispatch"]
        candidate = candidates[dispatch["candidate_id"]]
        checked = admit(candidate, envelope, dispatch["observation"], dispatch["notice"], study["scheduled_s"],
                        expected_deployment_binding=deployment_binding)
        checks["fresh_dispatch_admission"] = (checked["accepted"] and checked == dispatch["check"]
            and dispatch["revision"] == revisions[-1]["revision"]
            and dispatch["candidate_id"] == revisions[-1]["candidate_id"])
        requested = [row for row in study["execution"]["events"] if row["event"] == "return_requested"]
        checks["observed_dispatch"] = (len(requested) == 1
            and abs(requested[0]["time_s"]-dispatch["time_s"]) <= 1e-6
            and abs(study["execution"]["return_controller"]["return_time_s"]-candidate["return_time_s"]) <= 1e-6)
        checks["clock_advanced_during_planning"] = all(
            row["end_time_s"]-row["start_time_s"]+1e-6 >= row["wall_time_s"] for row in study["pending_operations"])
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        errors.append(str(error))
        checks["planning_record_complete"] = False
    return {"passed": bool(checks) and all(checks.values()), "checks": checks, "errors": errors,
            "scope": "orbital_planning_and_dispatch_only", "fault_recovery_qualified": False}


def expand_sensor_guard_checks(control):
    """Restore exact Guard inputs from storage v2, or read legacy v1 unchanged."""
    from . import starship_ship_capability_observation as obs
    from . import starship_ship_online_prediction as prediction
    from .starship_ship_online_guard import BUNDLE_KEYS
    schema = control.get("schema")
    rows = control.get("guard_checks")
    if type(rows) is not list:
        raise ValueError("sensor_storage_guard_rows_required")
    if schema == "missionos.starship_m3_sensor_control.v1":
        if any("bundle_sha256" in row or "forecast_refs" in row.get("bundle", {}) for row in rows):
            raise ValueError("sensor_storage_mixed_legacy_references")
        return rows
    if schema != "missionos.starship_m3_sensor_control.v2":
        raise ValueError("sensor_storage_schema")
    if "controller_audit" in control or "storage_fallback" in control:
        raise ValueError("sensor_storage_mixed_v2")
    forecasts = control.get("forecasts")
    if type(forecasts) is not list or len(forecasts) not in (0, 6):
        raise ValueError("sensor_storage_forecast_table_length")
    ids, refs = [], []
    for forecast in forecasts:
        if (type(forecast) is not dict or set(forecast) != prediction.FORECAST_KEYS
                or forecast["forecast_id"] != obs.digest({k: v for k, v in forecast.items() if k != "forecast_id"})):
            raise ValueError("sensor_storage_forecast_content_id")
        ids.append(forecast["forecast_id"])
        refs.append(obs.digest(forecast))
    if len(set(ids)) != len(ids):
        raise ValueError("sensor_storage_duplicate_forecast")
    expanded = []
    for row in rows:
        if row.get("kind") == "tracking_precondition":
            if set(row) != {"kind", "receipt"}:
                raise ValueError("sensor_storage_tracking_row_shape")
            expanded.append(row)
            continue
        if (row.get("kind") != "separate_process"
                or set(row) != {"kind", "bundle", "bundle_sha256", "receipt"}):
            raise ValueError("sensor_storage_guard_row_shape")
        bundle = row["bundle"]
        if (type(bundle) is not dict or set(bundle) != BUNDLE_KEYS-{"forecasts"}|{"forecast_refs"}
                or len(forecasts) != 6 or bundle["forecast_refs"] != refs):
            raise ValueError("sensor_storage_ordered_forecast_references")
        restored = {k: v for k, v in bundle.items() if k != "forecast_refs"}
        restored["forecasts"] = forecasts
        if obs.digest(restored) != row["bundle_sha256"]:
            raise ValueError("sensor_storage_guard_bundle_hash")
        expanded.append({"kind": row["kind"], "bundle": restored, "receipt": row["receipt"]})
    return expanded


def verify_sensor(study, *, authoritative_capture, expected_config, expected_mode=None):
    """Independently check actual contact and replay the separate guard's inputs."""
    from . import starship_ship_capability_observation as obs
    from . import starship_ship_capability_guard as prior
    from .starship_ship_online_guard import check_bundle
    from .starship_sixdof_mission import vehicle
    from ..intelligence import starship_ship_sensor_agent as agent
    checks, errors, ai_completed = {}, [], False
    try:
        control = study["sensor_control"]
        guard_checks = expand_sensor_guard_checks(control)
        record = control["record"]
        verified = obs.verify_record(record, expected_config["contact_limits"])
        stronger = prior.stronger_record_checks(record, study["profile"], 0)
        checks["independent_execution_record"] = verified["record_valid"] and stronger["passed"]
        checks["observed_contact_limits"] = verified["contact_limits_met"]
        checks["physical_factor_frozen"] = record["plant_factor"] == expected_config["physical_factor"]
        checks["same_return_end"] = (record["final_state"] == study["execution"]["final_state"]
            and record["outcome"]["contact_receipt"] == study["execution"]["outcome"]["contact_receipt"])
        initial = record["initial_state"]
        checks["actual_dispatch_origin"] = (initial["time_s"] == study["dispatch"]["time_s"]
            and any(all(sample.get(k) == value for k, value in initial.items())
                    for sample in study["execution"]["samples"]))
        checks["sensor_finished"] = control["complete"] is True and control.get("error") is None
        checks["harness_frozen"] = control["fault_harness"] == {"trigger": "accepted_return_dispatch", **expected_config}
        nominal = vehicle(study["profile"], payload_count=0)
        measured = obs.estimate(obs.prepare_telemetry(control["accelerometer_telemetry"], nominal), control["complete_time_s"])
        checks["burn_estimate_recomputed"] = control["initial_estimate"] == measured
        attached = [row for row in control["milestones"] if row["event"] == "m3_sensor_attached"]
        checks["attachment_history_bound"] = (len(attached) == 1
            and attached[0]["time_s"] == initial["time_s"]
            and attached[0]["state_sha256"] == obs.digest(initial)
            and attached[0]["controller_sha256"] == obs.digest(control["attachment_controller"])
            and attached[0]["handoff_sha256"] == study["deployment_binding"]["handoff_sha256"])
        requests = {agent.digest(req): agent.validate_request(req) for req in control["requests"]}
        envelopes = {(env["request_sha256"], env["provider"]): agent.validate_envelope(env)
                     for env in control.get("envelopes", [])}
        decisions_valid = (len(requests) == len(control["requests"])
            and len(envelopes) == len(control.get("envelopes", [])) == len(control["decisions"])
            and sum(req["tracking_observation"] is None for req in control["requests"]) <= 2
            and sum(req["tracking_observation"] is not None for req in control["requests"]) <= 2)
        real_ids = set()
        for index, decision in enumerate(control["decisions"]):
            env = envelopes[(decision["request_sha256"], decision["provider"])]
            reply = decision["reply"]
            decisions_valid &= (decision["decision_id"] == obs.digest({k: v for k, v in decision.items() if k != "decision_id"})
                and env["request_sha256"] in requests and env["request"] == requests[env["request_sha256"]]
                and env["authorization_sha256"] == obs.digest(expected_config) and env["run_id"] == study["run_id"])
            if env["provider"] == "deepseek":
                previous = control["decisions"][index-1] if index else {}
                decisions_valid &= (previous.get("provider") == "typesafe"
                    and previous.get("accepted_action") == agent.DEEP_REASONING_ACTION
                    and previous.get("request_sha256") == decision["request_sha256"])
            if decision["accepted_action"] is None:
                continue
            receipt = reply.get("provider_receipt") or {}
            bound = (reply.get("schema") == agent.DECISION_SCHEMA and reply.get("run_id") == study["run_id"]
                and reply.get("request_sha256") == env["request_sha256"]
                and reply.get("request_id") == env["request"]["request_id"]
                and reply.get("action") == decision["accepted_action"] in env["eligible_actions"]
                and decision["host_elapsed_s"] <= env["decision_budget_s"]
                and receipt.get("provider") == env["provider"])
            fixture = (expected_mode != "live" and reply.get("status") == "transport_fixture" and reply.get("model_inference_invoked") is False
                and receipt.get("call_attempted") is False and receipt.get("status") == "transport_fixture")
            real = (reply.get("status") == "valid" and reply.get("model_inference_invoked") is True
                and all(receipt.get(k) is True for k in ("call_attempted", "call_succeeded", "model_inference_invoked",
                    "complete_response_observed", "response_received"))
                and receipt.get("status") == "succeeded" and receipt.get("retries") == 0
                and receipt.get("public_input_sha256") == agent.digest(agent.public_input(env))
                and receipt.get("request_sha256") == agent.digest(agent.build_payload(env))
                and receipt.get("model_id") == receipt.get("requested_model_id") == agent.build_payload(env)["model"])
            refs = reply.get("reference_ids", [])
            cited = env["provider"] != "deepseek" or (type(refs) is list and 1 <= len(refs) <= 16
                and len(set(refs)) == len(refs) and all(ref in env["request"]["evidence"] for ref in refs))
            decisions_valid &= bool(bound and cited and (fixture or real))
            if bound and cited and real:
                real_ids.add(decision["decision_id"])
        checks["decision_receipts_rechecked"] = bool(decisions_valid)
        normal = expected_config["physical_factor"] == expected_config["accelerometer_gain"] == 1.
        if normal:
            checks["normal_has_no_sensor_action"] = (not control["requests"] and not control["decisions"]
                and not control["forecasts"] and not control["guard_checks"] and not control["uses"]
                and record["switch"] is None and not authoritative_capture and measured["anomaly"] is False)
        else:
            captures = authoritative_capture
            if (type(captures) is not list or not 1 <= len(captures) <= 3
                    or any(type(row) is not dict or set(row) != {"payload", "authority"} for row in captures)):
                raise ValueError("independent_host_capture_required")
            by_hash = {obs.digest(row["authority"]): row for row in captures}
            checks["host_capture_bound"] = (len(by_hash) == len(captures)
                and control["capture_authority"] == captures[-1]["authority"]
                and all(row["authority"]["run_id"] == study["run_id"]
                    and row["authority"]["handoff_sha256"] == study["deployment_binding"]["handoff_sha256"]
                    and row["authority"]["snapshot_sha256"] == row["payload"]["snapshot"]["snapshot_id"]
                    and row["authority"]["request_snapshot_sha256"] == row["payload"]["request_snapshot"]["snapshot_id"]
                    and row["payload"]["source_sha256"] == row["authority"]["source_sha256"]
                    and row["payload"]["backend"] == row["authority"]["backend"] for row in captures))
            admitted = {}
            for row in guard_checks:
                if row.get("kind") == "tracking_precondition":
                    receipt = row["receipt"]
                    req = next((req for req in control["requests"] if obs.digest(req) == receipt["request_sha256"]), None)
                    expected = {"schema": "missionos.ship_sensor_tracking_precondition.v1", "passed": False,
                        "request_sha256": receipt["request_sha256"], "selected_action": receipt["selected_action"],
                        "checks": {"requested_independent_tracking_available": False},
                        "scope": "online_discrete_predictions_same_model_not_qualification"}
                    expected["receipt_id"] = obs.digest(expected)
                    if req is None or req["tracking_observation"] is not None or receipt != expected:
                        errors.append("tracking_precondition_receipt_invalid")
                    continue
                bundle, receipt = row["bundle"], row["receipt"]
                captured = by_hash.get(bundle["authority_sha256"])
                if captured is None:
                    raise ValueError("guard_authority_not_independently_captured")
                payload, authority = captured["payload"], captured["authority"]
                replayed = check_bundle(bundle, authority)
                if replayed != receipt:
                    errors.append("guard_receipt_replay_mismatch")
                if (bundle["snapshot"] != payload["snapshot"]
                        or bundle["request_snapshot"] != payload["request_snapshot"]
                        or bundle["tracking_delivery"] != payload["tracking_delivery"]):
                    errors.append("guard_capture_substitution")
                if replayed.get("passed"):
                    admitted[replayed["receipt_id"]] = replayed
            checks["guard_receipts_independent"] = bool(control["guard_checks"]) and not errors
            uses = control["uses"]
            checks["one_bound_application"] = len(uses) == 1 and uses[0]["receipt_id"] in admitted
            if checks["one_bound_application"]:
                use, receipt = uses[0], admitted[uses[0]["receipt_id"]]
                checks["application_fields"] = (use["consumed_once"] is True and use["plant_unchanged"] is True
                    and use["time_s"] == receipt["application_time_s"] == control["application_time_s"]
                    and use["selected_action"] == receipt["selected_action"]
                    and use["request_sha256"] == receipt["request_sha256"])
                switch = record["switch"]
                checks["actual_controller_effect"] = (switch is None if use["selected_action"] == "continue_nominal_assumption"
                    else switch == {"time_s": use["time_s"], "factor": receipt["factor_estimate"]})
                executed = [row for row in control["milestones"] if row["event"] == "m3_sensor_action_executed"]
                checks["application_event_bound"] = (len(executed) == 1
                    and all(executed[0].get(k) == value for k, value in use.items()))
                decision = next((row for row in control["decisions"] if row.get("decision_id") == use.get("decision_id")), None)
                tracking_ai = any(row["accepted_action"] == agent.TRACKING_ACTION
                    and row["decision_id"] in real_ids and requests[row["request_sha256"]]["tracking_observation"] is None
                    for row in control["decisions"])
                ai_completed = (not control["fallback"] and decision is not None
                    and decision.get("accepted_action") == use["selected_action"]
                    and decision["decision_id"] in real_ids and tracking_ai)
        result = {"passed": bool(checks) and all(checks.values()), "checks": checks, "errors": errors,
                  "contact": verified.get("contact"), "record_verification": verified,
                  "stronger_record_checks": stronger, "ai_control_completed": bool(ai_completed),
                  "physical_execution": False, "general_qualification": False}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        errors.append(str(error))
        result = {"passed": False, "checks": checks, "errors": errors,
                  "ai_control_completed": False, "physical_execution": False, "general_qualification": False}
    return result

"""Experimental capability adoption through observed estimates and a separate guard.

No provider secret enters this module's worker. Physical truth is confined to
the plant and sensor synthesis; numerical tools and models receive observations.
"""
from copy import deepcopy
from dataclasses import asdict
import gzip
import json
import os
import subprocess
import sys
import time

from . import starship_ship_capability_observation as obs
from . import starship_ship_capability_guard as guard
from .starship_return_feasibility import backend

APPLICATION_DELAY_S = 600.0
CONTACT_LIMITS = {"speed_mps": 5.0, "tilt_deg": 5.0, "body_rate_rad_s": .02, "reserve_kg": 28000.0}
SECRET_NAMES = ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY", "JEV_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")


def json_value(value):
    if hasattr(value, "__dict__"):
        value = vars(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def controller_history(controller):
    # Do not serialize the audit or the plant limits into a decision request.
    return json_value(vars(controller.controller))


def snapshot_for(state, controller):
    return {"state": json_value(asdict(state)), "phase": controller.phase,
            "return_status": controller.controller.record["status"],
            "switch": deepcopy(controller.switch),
            "controller_history_sha256": obs.digest(controller_history(controller))}


def worker_environment(root):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root),
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}


def require_secret_free():
    if any(k in os.environ for k in SECRET_NAMES) or any(
        any(part in k.upper() for part in ("API_KEY", "SECRET", "TOKEN", "CREDENTIAL")) for k in os.environ
    ):
        raise ValueError("credential_forbidden_in_capability_worker")


def envelope(request, run_id, authority, *, provider=None):
    complete = request["burn_data_complete"]
    eligible = list(request["allowed_actions"])
    if not complete:
        eligible = [a for a in eligible if a != "adopt_estimated_capability"]
    routing = authority.get("planning_routing")
    if routing not in (None, "jev_primary_optional_deepseek_v1"):
        raise ValueError("unknown_capability_planning_routing")
    optional = (routing == "jev_primary_optional_deepseek_v1"
                and complete and request["capability_estimate"]["anomaly"])
    if provider is not None and not (optional and provider == "deepseek"):
        raise ValueError("capability_provider_override_not_authorized")
    provider = provider or ("typesafe" if optional else
        "deepseek" if complete and request["capability_estimate"]["anomaly"] else "typesafe")
    if optional and provider == "typesafe":
        eligible.append("request_deep_reasoning")
    return {"schema": "missionos.ship_capability_step3_request.v2" if optional else
                "missionos.ship_capability_step3_request.v1", "run_id": run_id,
            "request": request, "request_sha256": obs.digest(request), "eligible_actions": eligible,
            "application_time_s": request["issued_at_s"]+APPLICATION_DELAY_S if complete else None,
            "decision_budget_s": 150.0 if provider == "deepseek" else 35.0,
            "authorization_sha256": obs.digest(authority), "provider": provider}


def fixture_reply(reply):
    return (type(reply) is dict and reply.get("status") == "transport_fixture"
        and reply.get("model_inference_invoked") is False
        and type(reply.get("provider_receipt")) is dict
        and reply.get("provider_receipt", {}).get("call_attempted") is False
        and reply["provider_receipt"].get("call_succeeded", False) is False
        and reply["provider_receipt"].get("model_inference_invoked", False) is False
        and reply.get("provider_receipt", {}).get("status") == "transport_fixture")


def reference_check(reply, request, *, allow_fixture=False):
    if type(reply) is not dict:
        return {"valid": False, "resolved": []}
    if not reply.get("model_inference_invoked") and not (allow_fixture and fixture_reply(reply)):
        return {"valid": False, "resolved": []}
    ids = reply.get("reference_ids", [])
    data = obs.public_input(request)["evidence"]
    valid = (type(ids) is list and 1 <= len(ids) <= 16
             and all(type(i) is str and i in data for i in ids) and len(set(ids)) == len(ids))
    return {"valid": valid, "resolved": [{"reference_id": i, "value": data[i]} for i in ids] if valid else []}


def proposed_action(reply, env, *, allow_fixture=False):
    if (type(reply) is not dict or reply.get("run_id") != env["run_id"] or reply.get("request_id") != env["request"]["request_id"]
            or reply.get("request_sha256") != env["request_sha256"]
            or reply.get("action") not in env["eligible_actions"]):
        return None
    if env["schema"] == "missionos.ship_capability_step3_request.v2":
        provider_receipt = reply.get("provider_receipt")
        if (reply.get("schema") != "missionos.ship_capability_step3_decision.v2"
                or type(provider_receipt) is not dict
                or provider_receipt.get("provider") != env["provider"]):
            return None
        if not (allow_fixture and fixture_reply(reply)) and (
                reply.get("status") != "valid" or provider_receipt.get("status") != "succeeded"
                or any(provider_receipt.get(key) is not True for key in
                    ("call_attempted", "call_succeeded", "model_inference_invoked"))):
            return None
    if reply.get("status") == "transport_fixture" and not (allow_fixture and fixture_reply(reply)):
        return None
    if reply.get("model_inference_invoked") is not True and not (allow_fixture and fixture_reply(reply)):
        return None
    if env["provider"] == "deepseek" and not reference_check(reply, env["request"], allow_fixture=allow_fixture)["valid"]:
        return None
    return reply["action"]


def assess_decision(request, run_id, authority, decide, *, allow_fixture=False, provider=None, clock=None):
    """Record one distinct judgment; routing choices never authorize adoption."""
    clock = time.monotonic if clock is None else clock
    env = envelope(request, run_id, authority, provider=provider)
    started = clock()
    reply = decide(env)
    elapsed = clock()-started
    if type(reply) is not dict:
        reply = {"status": "malformed_host_reply", "model_inference_invoked": False}
    action = proposed_action(reply, env, allow_fixture=allow_fixture) if elapsed <= env["decision_budget_s"] else None
    if reply.get("model_inference_invoked") and elapsed > env["decision_budget_s"]:
        reply = {**reply, "status": "late_answer_not_adopted"}
    planning = request["burn_data_complete"] and request["capability_estimate"]["anomaly"]
    routing = action == "request_deep_reasoning"
    row = {"request_sha256": obs.digest(request), "issued_at_s": request["issued_at_s"],
        "request_schema": env["schema"], "provider": env["provider"],
        "reply": reply, "accepted_action": action, "host_elapsed_s": elapsed,
        "role": "planning_router" if routing else "planning" if planning
            else "normal_single_action_monitor" if request["burn_data_complete"] else "advisory_burn_monitor",
        "affects_plan": planning and action in ("continue_nominal_assumption", "adopt_estimated_capability"),
        "affects_route": routing,
        "reference_audit": reference_check(reply, request, allow_fixture=allow_fixture)
            if env["provider"] == "deepseek" else None}
    row["decision_id"] = obs.digest(row)
    return env, row


def planning_choice(request, authority, decision, *, planning_started, deadline, clock=None):
    """Respect Jev's final choice or its explicit one-time escalation request."""
    clock = time.monotonic if clock is None else clock
    optional = authority.get("planning_routing") == "jev_primary_optional_deepseek_v1"
    if optional:
        available = min(planning_started+APPLICATION_DELAY_S, deadline)-clock()
        if available < 35.0+45.0+2.0:
            return {"selected_action": None, "first_decision_id": None, "first_action": None,
                "first_provider": None, "final_decision_id": None, "final_provider": None,
                "final_action": None, "escalation_requested": False,
                "branch_effect": "jev_declined_for_headroom_checked_fallback_required", "events": [{
                    "event": "capability_primary_judgment_declined", "available_wall_s": available,
                    "required_wall_s": 35.0+45.0+2.0, "effect": "checked_fallback_required"}]}
    first = decision(request)
    selected, final = first["accepted_action"], first
    escalated = selected == "request_deep_reasoning"
    events = []
    effect = "physical_choice_selected" if selected is not None else "checked_fallback_required"
    if escalated:
        if authority.get("planning_routing") != "jev_primary_optional_deepseek_v1":
            raise ValueError("capability_escalation_not_authorized")
        available = min(planning_started+APPLICATION_DELAY_S, deadline)-clock()
        required = 150.0+45.0+2.0
        if available >= required:
            final = decision(request, provider="deepseek")
            selected = final["accepted_action"]
            effect = "explicit_deepseek_judgment" if selected is not None else "deepseek_missing_checked_fallback_required"
        else:
            selected, final = None, None
            effect = "deepseek_declined_for_headroom_checked_fallback_required"
        events.append({"event": "capability_deep_reasoning_requested", "decision_id": first["decision_id"],
            "available_wall_s": available, "required_wall_s": required,
            "deepseek_invoked": final is not None, "effect": effect})
    if selected not in ("continue_nominal_assumption", "adopt_estimated_capability"):
        selected, final = None, None
    return {"selected_action": selected, "first_decision_id": first["decision_id"],
        "first_action": first["accepted_action"], "first_provider": first["provider"],
        "final_decision_id": final["decision_id"] if final is not None else None,
        "final_provider": final["provider"] if final is not None else None,
        "final_action": selected, "escalation_requested": escalated, "branch_effect": effect,
        "events": events}


def consume(receipt, consumed, request, state, controller, authority, run_id):
    """Fresh executor-side, one-use binding; no new physical qualification."""
    identifier = receipt.get("receipt_id")
    unsigned = {k: v for k, v in receipt.items() if k != "receipt_id"}
    valid = (receipt.get("passed") is True and identifier == obs.digest(unsigned)
        and identifier not in consumed and receipt.get("run_id") == run_id
        and receipt.get("authority_sha256") == obs.digest(authority)
        and receipt.get("request_sha256") == obs.digest(request)
        and receipt.get("estimate_id") == request["capability_estimate"]["estimate_id"]
        and receipt.get("factor_estimate") == request["capability_estimate"]["factor_estimate"]
        and abs(state.time_s-receipt.get("application_time_s", -1)) < 1e-8
        and state.time_s <= request["deadline_s"]
        and controller.phase == "ballistic_return"
        and controller.controller.record["status"] == "active" and controller.switch is None)
    if not valid:
        raise ValueError("invalid_stale_or_reused_capability_receipt")
    consumed.add(identifier)
    return {"receipt_id": identifier, "consumed_once": True,
            "at_s": state.time_s, "current_snapshot_sha256": obs.digest(snapshot_for(state, controller)),
            "factor_estimate": receipt["factor_estimate"]}


def run_guard(bundle, authority, out, root):
    path, result = out/"guard-input.json.gz", out/"guard-verdict.json"
    wire = obs.encoded({"bundle": bundle, "authority": authority})
    path.write_bytes(gzip.compress(wire, compresslevel=3, mtime=0))
    command = [sys.executable, "-m", "src.runtime.starship_ship_capability_guard",
               "--output", str(result)]
    try:
        p = subprocess.run(command, cwd=root, env=worker_environment(root), input=wire,
                           capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        rejection = {"passed": False, "error": "guard_process_failed_or_timed_out"}
        result.write_bytes(obs.encoded(rejection))
        return rejection
    if not result.exists():
        return {"passed": False, "error": "guard_process_no_verdict", "exit_code": p.returncode}
    try:
        return json.loads(result.read_bytes())
    except (ValueError, OSError):
        return {"passed": False, "error": "guard_process_invalid_verdict"}


def prediction(state, controller, nominal, factor, estimate, profile, application_time, deadline, end_time_s):
    initial, ctrl = deepcopy(state), deepcopy(controller)
    plant = obs.model_vehicle(nominal, factor)
    ctrl.rebind_audit(plant)
    started = time.monotonic()

    def apply(s, c, events):
        if c.switch is None and s.time_s >= application_time-1e-9:
            c.apply_switch(s.time_s, estimate["factor_estimate"],
                           {"kind": "numeric_point_estimate", "estimate_id": estimate["estimate_id"]})
        return True

    run = obs.propagate(initial, plant, ctrl, profile, end_time_s=end_time_s,
                        deadline=min(deadline, started+180), before_update=apply)
    record = obs.record("step3-estimated-band-prediction", json_value(asdict(state)), plant, nominal, ctrl, run,
        plant_factor=factor, switch={"factor": estimate["factor_estimate"],
                                    "time_s": ctrl.switch["time_s"] if ctrl.switch else None},
        wall_time_s=time.monotonic()-started)
    return json_value(record)


def execute(origin, case, authority, out, root, decide, *, deadline, prediction_cache=None, prior_request=None):
    """One physical suffix with causal sensing, actual HTTP judgments and guarded adoption."""
    from scripts.run_starship_ship_fault_step2 import setup

    require_secret_free()
    factor = 1.0 if case == "normal_live" else .8
    state, plant, nominal, controller = setup(origin, factor)
    initial = json_value(asdict(state))
    rows, requests, decisions, forecasts, use = [], [], [], [], []
    seen_first, completed = False, False
    receipt, scheduled_request, selected, planning_judgment = None, None, None, None
    consumed, runtime_events = set(), []
    application_time, planning_started = None, None
    run_id = authority["experiment_id"]+"-"+case
    allow_fixture = authority.get("execution_mode") == "fixture_preflight"

    def decision(request, *, provider=None):
        env, row = assess_decision(request, run_id, authority, lambda env: decide(env, case),
                                   allow_fixture=allow_fixture, provider=provider)
        requests.append(env)
        decisions.append(row)
        return row

    def before(s, c, events):
        nonlocal seen_first, completed, receipt, scheduled_request, selected, application_time, planning_started, planning_judgment
        if time.monotonic() >= deadline:
            raise InterruptedError("step3_cohort_deadline")
        cut = obs.cutoff_time(events)
        if rows and not seen_first and not completed:
            measured = obs.measure(rows, 1 if factor == .8 else 0, 0)
            current = obs.estimate(obs.prepare_telemetry(measured, nominal), s.time_s)
            if current["anomaly"]:
                seen_first = True
                request = obs.build_request(run_id+"-burn", obs.observation(s, plant, c.phase), current,
                                            "propulsion_capability_anomaly", burn_complete=False)
                action = decision(request)["accepted_action"]
                runtime_events.append({"time_s": s.time_s, "event": "burn_observation_judged",
                    "action": action, "effect": "wait_for_complete_burn_data_no_switch", "advisory_only": True})
        if cut is not None and not completed and s.time_s >= cut+2.-1e-9:
            completed = True
            measured = obs.measure(rows, 1 if factor == .8 else 0, 0)
            current = obs.estimate(obs.prepare_telemetry(measured, nominal), s.time_s)
            request = obs.build_request(run_id+"-complete", obs.observation(s, plant, c.phase), current,
                                       "burn_data_complete", burn_complete=True)
            if not current["anomaly"]:
                action = decision(request)["accepted_action"]
                runtime_events.append({"time_s": s.time_s, "event": "normal_capability_monitored",
                                       "action": action, "effect": "nominal_plan_unchanged"})
                return True
            planning_started = time.monotonic()
            application_time = s.time_s+APPLICATION_DELAY_S
            snapshot = snapshot_for(s, c)
            points = (("band_low", current["band"][0]), ("point_estimate", current["factor_estimate"]),
                      ("band_high", current["band"][1]))
            preview = {"adopt_estimated_capability": {"plant_capability_basis": "estimated band points; not known capability", "points": []}}
            for label, value in points:
                cached = next((p for p in (prediction_cache or []) if p["label"] == label), None)
                if cached is not None:
                    if (cached["snapshot_sha256"] != obs.digest(snapshot) or cached["factor"] != value
                            or cached["application_time_s"] != application_time
                            or cached["record_sha256"] != obs.digest(cached["record"])
                            or cached["record"]["switch"]["factor"] != current["factor_estimate"]):
                        raise ValueError("prediction_cache_input_mismatch")
                    record = deepcopy(cached["record"])
                else:
                    record = prediction(s, c, nominal, value, current, origin["profile"], application_time,
                                        deadline, initial["time_s"]+3600.)
                verification = obs.verify_record(record, CONTACT_LIMITS)
                forecasts.append({"label": label, "factor": value, "record": record,
                    "record_sha256": obs.digest(record), "snapshot_sha256": obs.digest(snapshot),
                    "application_time_s": application_time})
                contact = verification["contact"] or {k: None for k in CONTACT_LIMITS}
                preview["adopt_estimated_capability"]["points"].append({"label": label,
                    "plant_capability_factor": value, **{k: contact[k] for k in CONTACT_LIMITS},
                    "contact_speed_mps": contact["speed_mps"], "limits_met": verification["contact_limits_met"]})
            # Reuse reviewed, Git-tracked comparison material, not another
            # private worktree's raw output. This cannot authorize adoption.
            if (prior_request is None or prior_request["observation"] != request["observation"]
                    or prior_request["capability_estimate"] != current):
                raise ValueError("continue_prediction_input_mismatch")
            preview["continue_nominal_assumption"] = deepcopy(prior_request["previews"]["continue_nominal_assumption"])
            request["previews"] = preview
            obs.validate_request(request)
            planning_judgment = planning_choice(request, authority, decision,
                planning_started=planning_started, deadline=deadline)
            selected = planning_judgment["selected_action"]
            for event in planning_judgment["events"]:
                runtime_events.append({"time_s": s.time_s, **event})
            measured_time = time.monotonic()-planning_started
            bundle = {"schema": "missionos.ship_capability_guard_bundle.v1", "run_id": run_id,
                "authority_sha256": obs.digest(authority), "request": request,
                "telemetry": [r for r in measured if r["time_s"]+obs.SENSOR["telemetry_latency_s"] <= s.time_s+1e-9],
                "nominal_profile": origin["profile"], "retained_count": origin["origin"]["retained_count"],
                "snapshot": snapshot, "predictions": forecasts, "source_sha256": authority["source_sha256"],
                "backend": backend(), "checked_at_s": s.time_s+measured_time,
                "application_time_s": application_time}
            guard_headroom = min(planning_started+APPLICATION_DELAY_S, deadline)-time.monotonic()
            if (authority.get("planning_routing") == "jev_primary_optional_deepseek_v1"
                    and guard_headroom < 45.0+2.0):
                receipt = {"passed": False, "error": "planning_guard_headroom_insufficient"}
                runtime_events.append({"time_s": s.time_s, "event": "capability_guard_declined",
                    "available_wall_s": guard_headroom, "required_wall_s": 45.0+2.0})
            else:
                receipt = run_guard(bundle, authority, out, root)
            total_wall = time.monotonic()-planning_started
            if total_wall > APPLICATION_DELAY_S-2:
                receipt = {"passed": False, "error": "planning_window_expired"}
            scheduled_request = request
            fallback = selected is None
            if fallback and receipt.get("passed"):
                selected = "adopt_estimated_capability"
            runtime_events.append({"time_s": s.time_s, "event": "capability_plan_checked",
                "selected_action": selected, "fallback": fallback, "guard_passed": receipt.get("passed", False),
                "planning_wall_s": total_wall, "reserved_simulation_s": APPLICATION_DELAY_S,
                "application_time_s": application_time, "criterion": "measured_work_fits_reserved_coast_window",
                "planning_decision_id": planning_judgment["final_decision_id"] if not fallback else None,
                "planning_provider": planning_judgment["final_provider"] if not fallback else None})
        if application_time is not None and c.switch is None and s.time_s >= application_time-1e-9:
            if selected == "adopt_estimated_capability" and receipt.get("passed"):
                binding = consume(receipt, consumed, scheduled_request, s, c, authority, run_id)
                binding["planning_decision_id"] = (planning_judgment["final_decision_id"]
                    if planning_judgment and planning_judgment["final_action"] == "adopt_estimated_capability" else None)
                before_limits = [e.max_thrust_n for e in plant.engines]
                c.apply_switch(s.time_s, receipt["factor_estimate"],
                    {"kind": "guarded_numeric_point_estimate", "estimate_id": receipt["estimate_id"],
                     "receipt_id": receipt["receipt_id"]})
                binding["plant_unchanged"] = before_limits == [e.max_thrust_n for e in plant.engines]
                use.append(binding)
                runtime_events.append({"time_s": s.time_s, "event": "estimated_capability_applied",
                                       "factor_estimate": receipt["factor_estimate"]})
                application_time = None
            else:
                runtime_events.append({"time_s": s.time_s, "event": "capability_plan_retained_by_judgment"
                    if selected == "continue_nominal_assumption" else "capability_adoption_not_admitted",
                                       "effect": "nominal_control_continues_without_safe_return_claim"})
                application_time = None
        return True

    def on_step(s, p, c):
        if c.phase in ("deorbit_burn", "ballistic_return") and not completed:
            rows.append(obs.specific_force_row(s, p, c.phase))

    started = time.monotonic()
    run = obs.propagate(state, plant, controller, origin["profile"],
        end_time_s=initial["time_s"]+3600., deadline=deadline, before_update=before, on_step=on_step)
    record = json_value(obs.record(case, initial, plant, nominal, controller, run, plant_factor=factor,
        switch=None if controller.switch is None else {"factor": controller.switch["factor"], "time_s": controller.switch["time_s"]},
        wall_time_s=time.monotonic()-started))
    verdict = obs.verify_record(record, CONTACT_LIMITS)
    strong = guard.stronger_record_checks(record, origin["profile"], origin["origin"]["retained_count"])
    result = {"schema": "missionos.ship_capability_step3_run.v1", "run_id": run_id, "case": case,
        "record": record, "verification": verdict, "stronger_record_checks": strong,
        "requests": requests, "decisions": decisions, "guard_receipt": receipt,
        "planning_judgment": planning_judgment,
        "receipt_uses": use, "runtime_events": runtime_events, "telemetry": obs.measure(rows, 1 if factor == .8 else 0, 0),
        "prediction_records": forecasts, "physical_execution": False,
        "general_fault_qualification": False, "nominal_certificate_used_for_fault": False,
        "worker_secret_free": True}
    return result

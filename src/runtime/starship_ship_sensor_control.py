"""Opt-in finite return experiment with causally requested independent tracking.

This worker has no provider credentials. Numerical estimates are tools; model
choices, guard admission, one-use execution and observed contact are separate.
"""

from copy import deepcopy
from dataclasses import asdict
import gzip
import json
import math
import subprocess
import sys
import time

from . import starship_ship_capability_observation as obs
from . import starship_ship_capability_control as old
from . import starship_ship_capability_guard as old_guard
from . import starship_ship_sensor_observation as sensor
from ..intelligence import starship_ship_sensor_agent as agent

CONTINUE, ADOPT, TRACK = agent.CONTINUE_ACTION, agent.ADOPT_ACTION, agent.TRACKING_ACTION
LIMITS = dict(old.CONTACT_LIMITS)


def estimate_pair(rows, tracking_rows, nominal, case, observed_at_s):
    measured = sensor.measure_accelerometer(
        [r for r in rows if r["phase"] == "deorbit_burn"],
        1 if case["physical_factor"] == 0.8 else 0,
        0,
        gain=case["accelerometer_gain"],
    )
    current = obs.estimate(obs.prepare_telemetry(measured, nominal), observed_at_s)
    samples = sensor.measure_tracking(tracking_rows, 0)
    return measured, current, samples


def request(
    run_id,
    state,
    plant,
    estimate,
    *,
    tracking=None,
    requested=False,
    initial_time_s,
    previews,
    guarded_reconsideration=False,
):
    observation = obs.observation(state, plant, "complete_burn")
    observation["tracking_requested"] = requested
    evidence = {
        "accel_factor": estimate["factor_estimate"],
        "accel_band_low": estimate["band"][0],
        "accel_band_high": estimate["band"][1],
        "accel_samples": estimate["accepted_samples"],
        "tracking_delay_s": 30.0,
        "application_time_s": initial_time_s + 600.0,
    }
    if tracking is not None:
        evidence.update(
            tracking_factor=tracking["factor_estimate"],
            tracking_band_low=tracking["band"][0],
            tracking_band_high=tracking["band"][1],
            tracking_intervals=tracking["accepted_samples"],
        )
    for key, value in previews["hypotheses"].items():
        for action, contact in value.items():
            evidence[key + "." + action + ".speed_mps"] = contact["speed_mps"]
            evidence[key + "." + action + ".limits_met"] = contact["limits_met"]
    value = {
        "schema": agent.OBSERVATION_SCHEMA,
        "request_id": run_id + ("-after-tracking" if tracking is not None else "-before-tracking"),
        "issued_at_s": state.time_s,
        "deadline_s": initial_time_s + 900.0,
        "observation": observation,
        "accelerometer_estimate": deepcopy(estimate),
        "tracking_observation": deepcopy(tracking),
        "previews": deepcopy(previews),
        "evidence": evidence,
    }
    if guarded_reconsideration:
        value.update(schema=agent.OBSERVATION_SCHEMA_V2, guard_feedback=None)
    return agent.validate_request(value)


def reconsideration_request(req, receipt, decision):
    """Return actual rejected Rules evidence; never manufacture a replacement judgment."""
    if (
        req.get("schema") != agent.OBSERVATION_SCHEMA_V2
        or req.get("guard_feedback") is not None
        or receipt.get("passed") is not False
        or receipt.get("request_sha256") != agent.digest(req)
        or receipt.get("selected_action") != decision.get("accepted_action")
        or decision.get("request_sha256") != agent.digest(req)
        or decision.get("decision_id")
        != obs.digest({k: v for k, v in decision.items() if k != "decision_id"})
        or receipt.get("receipt_id")
        != obs.digest({k: v for k, v in receipt.items() if k != "receipt_id"})
    ):
        raise ValueError("reconsideration_requires_bound_rejected_judgment")
    failed = [k for k, passed in receipt.get("checks", {}).items() if passed is False]
    if not failed:
        raise ValueError("reconsideration_requires_check_reasons")
    result = deepcopy(req)
    result["request_id"] += "-review-1"
    result["guard_feedback"] = {
        "schema": "missionos.ship_sensor_guard_feedback.v1",
        "rejected_action": decision["accepted_action"],
        "prior_request_sha256": agent.digest(req),
        "prior_decision_id": decision["decision_id"],
        "receipt_id": receipt["receipt_id"],
        "failed_checks": failed,
        "reconsideration": 1,
        "scope": receipt["scope"],
    }
    return agent.validate_request(result)


def assess(req, run_id, authority_hash, application_time_s, decide, *, allow_fixture, deadline):
    """One Jev choice plus only an explicitly requested optional DeepSeek choice."""
    rows, envelopes = [], []
    for provider in ("typesafe", "deepseek"):
        needed = (35.0 if provider == "typesafe" else 150.0) + 47.0
        if deadline - time.monotonic() < needed:
            return None, rows, envelopes
        env = {
            "schema": agent.REQUEST_SCHEMA,
            "run_id": run_id,
            "request": req,
            "request_sha256": agent.digest(req),
            "eligible_actions": agent.eligible_actions(req, provider),
            "application_time_s": application_time_s,
            "decision_budget_s": 35.0 if provider == "typesafe" else 150.0,
            "authorization_sha256": authority_hash,
            "provider": provider,
        }
        agent.validate_envelope(env)
        started = time.monotonic()
        reply = decide(env)
        if type(reply) is not dict:
            reply = {"status": "malformed_reply", "model_inference_invoked": False}
        elapsed = time.monotonic() - started
        accepted = None
        if type(reply) is dict:
            receipt = reply.get("provider_receipt", {})
            if type(receipt) is not dict:
                receipt = {}
            bound = (
                reply.get("schema") == agent.DECISION_SCHEMA
                and reply.get("run_id") == run_id
                and reply.get("request_sha256") == env["request_sha256"]
                and reply.get("request_id") == req["request_id"]
                and reply.get("action") in env["eligible_actions"]
                and elapsed <= env["decision_budget_s"]
                and receipt.get("provider") == provider
            )
            real = (
                reply.get("status") == "valid"
                and reply.get("model_inference_invoked") is True
                and receipt.get("call_attempted") is True
                and receipt.get("call_succeeded") is True
                and receipt.get("status") == "succeeded"
                and receipt.get("model_inference_invoked") is True
                and receipt.get("model_id") == agent.build_payload(env)["model"]
                and receipt.get("requested_model_id") == agent.build_payload(env)["model"]
                and receipt.get("public_input_sha256") == agent.digest(agent.public_input(env))
                and receipt.get("request_sha256") == agent.digest(agent.build_payload(env))
                and receipt.get("complete_response_observed") is True
                and receipt.get("response_received") is True
                and receipt.get("retries") == 0
            )
            fixture = (
                allow_fixture
                and reply.get("status") == "transport_fixture"
                and reply.get("model_inference_invoked") is False
                and receipt.get("call_attempted") is False
                and receipt.get("status") == "transport_fixture"
            )
            refs = reply.get("reference_ids", [])
            cited = (
                provider != "deepseek"
                or type(refs) is list
                and 1 <= len(refs) <= 16
                and all(type(k) is str and k in req["evidence"] for k in refs)
                and len(set(refs)) == len(refs)
            )
            if bound and (real or fixture) and cited:
                accepted = reply["action"]
        row = {
            "request_sha256": env["request_sha256"],
            "provider": provider,
            "accepted_action": accepted,
            "reply": reply,
            "host_elapsed_s": elapsed,
            "observation_stage": "after_tracking"
            if req["tracking_observation"]
            else "before_tracking",
            "reference_audit": {
                "resolved": [
                    {"reference_id": k, "value": req["evidence"][k]}
                    for k in reply.get("reference_ids", [])
                ]
                if accepted and provider == "deepseek"
                else [],
                "free_prose_verified": False,
                "internal_reasoning_verified": False,
            },
        }
        row["decision_id"] = obs.digest(row)
        rows.append(row)
        envelopes.append(env)
        if accepted != agent.DEEP_REASONING_ACTION:
            return accepted, rows, envelopes
        if provider == "deepseek":
            raise ValueError("recursive_deep_reasoning")
    return None, rows, envelopes


def run_guard(bundle, authority, out, root):
    wire = obs.encoded({"bundle": bundle, "authority": authority})
    (out / "guard-input.json.gz").write_bytes(gzip.compress(wire, compresslevel=3, mtime=0))
    try:
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.runtime.starship_ship_sensor_guard",
                "--output",
                str(out / "guard-verdict.json"),
            ],
            input=wire,
            capture_output=True,
            cwd=root,
            env=old.worker_environment(root),
            timeout=45,
        )
        if not (out / "guard-verdict.json").exists():
            return {"passed": False, "error": "guard_no_verdict", "exit_code": p.returncode}
        return json.loads((out / "guard-verdict.json").read_bytes())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"passed": False, "error": "guard_process_failed"}


def execute(
    origin,
    case,
    *,
    run_id,
    mode,
    out,
    root,
    deadline,
    source_sha256,
    backend,
    authority=None,
    previews=None,
    evidence=None,
    decide=None,
    screen_action=None,
):
    from scripts.run_starship_ship_fault_step2 import setup
    from . import starship_ship_sensor_guard as guard

    old.require_secret_free()
    state, plant, nominal, controller = setup(origin, case["physical_factor"])
    initial = old.json_value(asdict(state))
    rows, tracking_rows = [], []
    decisions, requests, events, uses = [], [], [], []
    guard_checks = []
    guarded = bool(authority and authority["schema"] == guard.AUTHORITY_SCHEMA_V2)
    complete_time = application_time = None
    delivery = scheduled = receipt = selected = None
    numeric = measured = samples = None
    first_started = None
    fallback = False
    done_final = False
    consumed = set()

    def choose(req):
        nonlocal fallback
        action, ds, es = assess(
            req,
            run_id,
            obs.digest(authority),
            application_time,
            decide,
            allow_fixture=mode == "preflight",
            deadline=min(deadline, first_started + 598.0 - 30.0),
        )
        decisions.extend(ds)
        requests.extend(es)
        if action is None:
            fallback = True
        return action

    def legacy_finalize(s, req, action):
        nonlocal selected, receipt, scheduled, done_final
        selected, scheduled, done_final = action, req, True
        if (
            time.monotonic() - first_started + (30.0 if delivery is not None else 0.0)
            > 598.0 - 45.0
        ):
            receipt = {"passed": False, "error": "guard_headroom_insufficient"}
            return
        bundle = {
            "schema": guard.BUNDLE_SCHEMA,
            "run_id": run_id,
            "authority_sha256": obs.digest(authority),
            "request": req,
            "accelerometer_telemetry": measured,
            "tracking_delivery": delivery,
            "nominal_profile": origin["profile"],
            "selected_action": action,
            "application_time_s": application_time,
            "checked_at_s": s.time_s,
            "source_sha256": source_sha256,
            "backend": backend,
            "evidence": evidence if action == ADOPT else [],
        }
        receipt = run_guard(bundle, authority, out, root)
        events.append(
            {
                "event": "sensor_plan_checked",
                "time_s": s.time_s,
                "selected_action": action,
                "fallback": fallback,
                "guard_passed": receipt.get("passed", False),
                "decision_id": decisions[-1]["decision_id"] if decisions and not fallback else None,
                "measured_work_s": time.monotonic() - first_started,
                "acquisition_latency_s": 30.0 if delivery is not None else 0.0,
            }
        )
        if time.monotonic() - first_started + (30.0 if delivery is not None else 0.0) > 598.0:
            receipt = {"passed": False, "error": "reserved_window_expired"}

    def acquire_tracking(s):
        nonlocal delivery
        if delivery is not None:
            raise ValueError("tracking_already_requested")
        delivery = sensor.request_tracking(samples, s.time_s, obs.digest(tracking_rows))
        events.append(
            {
                "event": "tracking_requested",
                "time_s": s.time_s,
                "received_at_s": delivery["received_at_s"],
                "fallback": fallback,
                "decision_id": decisions[-1]["decision_id"] if decisions and not fallback else None,
                "delivery_id": delivery["delivery_id"],
            }
        )

    def fallback_action(req):
        tracking = req["tracking_observation"]
        if tracking is None:
            return TRACK
        current = req["accelerometer_estimate"]
        return (
            ADOPT
            if tracking["band"][1] < 1.0
            and max(current["band"][0], tracking["band"][0])
            <= min(current["band"][1], tracking["band"][1])
            else CONTINUE
        )

    def check_proposal(s, req, action):
        """Each check keeps its own files, including rejected proposals and fallback."""
        elapsed = time.monotonic() - first_started
        latency = 30.0 if delivery is not None else 0.0
        if elapsed + latency > 598.0 - 45.0 or deadline - time.monotonic() < 45.0:
            result = {"passed": False, "error": "guard_headroom_insufficient"}
        else:
            bundle = {
                "schema": guard.BUNDLE_SCHEMA,
                "run_id": run_id,
                "authority_sha256": obs.digest(authority),
                "request": req,
                "accelerometer_telemetry": measured,
                "tracking_delivery": delivery,
                "nominal_profile": origin["profile"],
                "selected_action": action,
                "application_time_s": application_time,
                "checked_at_s": s.time_s,
                "source_sha256": source_sha256,
                "backend": backend,
                "evidence": evidence,
            }
            location = out / ("guard-check-%02d" % (len(guard_checks) + 1))
            location.mkdir(exist_ok=False)
            result = run_guard(bundle, authority, location, root)
        did = decisions[-1]["decision_id"] if decisions and not fallback else None
        guard_checks.append(
            {
                "request_sha256": agent.digest(req),
                "selected_action": action,
                "receipt": deepcopy(result),
                "decision_id": did,
                "fallback": fallback,
                "attempt": len(guard_checks) + 1,
            }
        )
        events.append(
            {
                "event": "sensor_plan_checked",
                "time_s": s.time_s,
                "selected_action": action,
                "fallback": fallback,
                "decision_id": did,
                "guard_passed": result.get("passed", False),
                "receipt_id": result.get("receipt_id"),
                "failed_checks": [k for k, v in result.get("checks", {}).items() if v is False],
                "measured_work_s": time.monotonic() - first_started,
                "acquisition_latency_s": latency,
            }
        )
        if time.monotonic() - first_started + latency > 598.0:
            return {"passed": False, "error": "reserved_window_expired"}
        return result

    def finalize(s, req, action):
        nonlocal selected, scheduled, receipt, done_final, fallback
        if not guarded:
            return legacy_finalize(s, req, action)
        # A physical proposal is checked before becoming an executable reservation.
        # One reconsideration per observation stage; repeated rejection ends AI repair.
        if action not in (CONTINUE, ADOPT):
            raise ValueError("physical_proposal_required")
        receipt = check_proposal(s, req, action)
        if not receipt.get("passed") and not fallback:
            try:
                revised = reconsideration_request(req, receipt, decisions[-1])
            except ValueError:
                revised = None
            if revised is not None:
                events.append(
                    {
                        "event": "sensor_guard_feedback",
                        "time_s": s.time_s,
                        "request_sha256": agent.digest(revised),
                        "feedback": deepcopy(revised["guard_feedback"]),
                    }
                )
                action = choose(revised)
                req = revised
                if action == TRACK:
                    acquire_tracking(s)
                    return
                if action in (CONTINUE, ADOPT):
                    receipt = check_proposal(s, req, action)
                else:
                    receipt = {"passed": False, "error": "reconsideration_no_judgment"}
        if not receipt.get("passed") and not fallback:
            fallback = True
        if not receipt.get("passed") and fallback:
            action = fallback_action(req)
            events.append(
                {
                    "event": "sensor_guarded_fallback",
                    "time_s": s.time_s,
                    "selected_action": action,
                    "basis": "preapproved_after_missing_or_rejected_judgment",
                }
            )
            if action == TRACK:
                acquire_tracking(s)
                return
            receipt = check_proposal(s, req, action)
        selected, scheduled, done_final = action, req, True

    def before(s, c, physical_events):
        nonlocal complete_time, application_time, numeric, measured, samples, delivery, selected
        nonlocal first_started
        cutoff = obs.cutoff_time(physical_events)
        if complete_time is None and cutoff is not None and s.time_s >= cutoff + 2.0 - 1e-9:
            complete_time = s.time_s
            application_time = complete_time + 600.0
            measured, numeric, samples = estimate_pair(rows, tracking_rows, nominal, case, s.time_s)
            if mode == "screen":
                selected = screen_action
            else:
                first_started = time.monotonic()
                req = request(
                    run_id,
                    s,
                    plant,
                    numeric,
                    initial_time_s=complete_time,
                    previews=previews,
                    guarded_reconsideration=guarded,
                )
                action = choose(req)
                if action in (TRACK, None):
                    acquire_tracking(s)
                else:
                    finalize(s, req, action)
        if (
            mode != "screen"
            and delivery is not None
            and not done_final
            and s.time_s >= delivery["received_at_s"]
        ):
            # Observations are exposed only after the requested acquisition completes.
            tracking = sensor.estimate_tracking(delivery, nominal, s.time_s)
            current = obs.estimate(obs.prepare_telemetry(measured, nominal), s.time_s)
            req = request(
                run_id,
                s,
                plant,
                current,
                tracking=tracking,
                requested=True,
                initial_time_s=complete_time,
                previews=previews,
                guarded_reconsideration=guarded,
            )
            events.append(
                {
                    "event": "tracking_delivered",
                    "time_s": s.time_s,
                    "delivery_id": delivery["delivery_id"],
                }
            )
            if fallback:
                action = (
                    ADOPT
                    if tracking["band"][1] < 1.0
                    and max(current["band"][0], tracking["band"][0])
                    <= min(current["band"][1], tracking["band"][1])
                    else CONTINUE
                )
            else:
                action = choose(req)
                if action is None:
                    action = (
                        ADOPT
                        if tracking["band"][1] < 1.0
                        and max(current["band"][0], tracking["band"][0])
                        <= min(current["band"][1], tracking["band"][1])
                        else CONTINUE
                    )
            finalize(s, req, action)
        if application_time is not None and s.time_s >= application_time - 1e-9 and not uses:
            if mode == "screen":
                if selected == ADOPT and c.switch is None:
                    c.apply_switch(
                        s.time_s, numeric["factor_estimate"], {"kind": "registered_counterfactual"}
                    )
                uses.append({"time_s": s.time_s, "screen": True, "action": selected})
            elif receipt and receipt.get("passed"):
                if guarded and (
                    not math.isclose(s.time_s, application_time, rel_tol=0.0, abs_tol=1e-8)
                    or time.monotonic() - first_started + (30.0 if delivery is not None else 0.0)
                    > 598.0
                    or time.monotonic() >= deadline
                ):
                    events.append(
                        {
                            "event": "sensor_qualified_execution_window_expired",
                            "time_s": s.time_s,
                            "reserved_application_time_s": application_time,
                            "receipt_id": receipt["receipt_id"],
                            "effect": "nominal_execution_without_safe_return_claim",
                        }
                    )
                    application_time = None
                    return True
                ok = guard.consume_receipt(
                    receipt,
                    consumed,
                    run_id=run_id,
                    request_sha256=agent.digest(scheduled),
                    authority_sha256=obs.digest(authority),
                    selected_action=selected,
                    application_time_s=application_time,
                )
                if not ok:
                    raise ValueError("sensor_guard_receipt_not_consumable")
                before_limits = [e.max_thrust_n for e in plant.engines]
                if selected == ADOPT:
                    c.apply_switch(
                        s.time_s,
                        receipt["factor_estimate"],
                        {"kind": "independent_tracking_guard", "receipt_id": receipt["receipt_id"]},
                    )
                uses.append(
                    {
                        "time_s": s.time_s,
                        "consumed_once": True,
                        "action": selected,
                        "receipt_id": receipt["receipt_id"],
                        "request_sha256": agent.digest(scheduled),
                        "decision_id": decisions[-1]["decision_id"]
                        if decisions and not fallback
                        else None,
                        "plant_unchanged": before_limits == [e.max_thrust_n for e in plant.engines],
                    }
                )
            else:
                events.append(
                    {
                        "event": "sensor_plan_not_admitted",
                        "time_s": s.time_s,
                        "action": selected,
                        "effect": "nominal_execution_without_safe_return_claim",
                    }
                )
                application_time = None
        return True

    def on_step(s, p, c):
        if complete_time is None and c.phase in ("deorbit_burn", "ballistic_return"):
            rows.append(obs.specific_force_row(s, p, c.phase))
            tracking_rows.append(sensor.tracking_source_row(s, c.phase))

    started = time.monotonic()
    result = obs.propagate(
        state,
        plant,
        controller,
        origin["profile"],
        end_time_s=state.time_s + 3600.0,
        deadline=deadline,
        before_update=before,
        on_step=on_step,
    )
    record = old.json_value(
        obs.record(
            run_id,
            initial,
            plant,
            nominal,
            controller,
            result,
            plant_factor=case["physical_factor"],
            switch=None
            if controller.switch is None
            else {"factor": controller.switch["factor"], "time_s": controller.switch["time_s"]},
            wall_time_s=time.monotonic() - started,
        )
    )
    verification = obs.verify_record(record, LIMITS)
    stronger = old_guard.stronger_record_checks(record, origin["profile"], 0)
    if mode == "screen":
        delivery = sensor.request_tracking(samples, complete_time, obs.digest(tracking_rows))
    return {
        "schema": "missionos.ship_sensor_run.v1",
        "mode": mode,
        "run_id": run_id,
        "case_id": case["case_id"],
        "record": record,
        "verification": verification,
        "stronger_record_checks": stronger,
        "complete_request_s": complete_time,
        "reserved_application_time_s": complete_time + 600.0 if complete_time else None,
        "accelerometer_estimate": numeric,
        "accelerometer_telemetry": measured,
        "accelerometer_source_rows": rows,
        "tracking_source_rows": tracking_rows,
        "tracking_samples": samples,
        "tracking_delivery": delivery,
        "decisions": decisions,
        "requests": requests,
        "runtime_events": events,
        "receipt_uses": uses,
        "guard_receipt": receipt,
        **({"guard_checks": guard_checks} if guarded else {}),
        "selected_action": selected,
        "fallback_used": fallback,
        "worker_secret_free": True,
        "physical_execution": False,
        "nominal_certificate_used": False,
        "general_qualification": False,
    }

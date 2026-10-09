"""Scalar M1 constraint/result checks, independent of the GNC and propagator.

These checks establish recorded constraints in a declared synthetic model, not
physical-model accuracy. No successful forecast flag or controller status is
accepted as a contact measurement.
"""

from hashlib import sha256
import json
import math


def checksum(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def metrics(trial):
    c = trial["outcome"]["contact_receipt"]
    if not c:
        return None
    w, x, y, z = c["q_body_to_eci"]
    axis = [2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)]
    tilt = math.degrees(
        math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(axis, c["surface_normal_eci"])))))
    )
    # ECI -> rotating WGS84 geodetic coordinates. Independent scalar equations.
    px, py, pz = c["point_eci_m"]
    theta = 7.292115e-5 * c["event_time_s"]
    ex, ey = (
        math.cos(theta) * px + math.sin(theta) * py,
        -math.sin(theta) * px + math.cos(theta) * py,
    )
    radial = math.hypot(ex, ey)
    e2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
    lat = math.atan2(pz, radial * (1 - e2))
    for _ in range(12):
        n = 6378137.0 / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(pz + e2 * n * math.sin(lat), radial)
    return {
        "time_s": c["event_time_s"],
        "speed_mps": c["surface_relative_speed_mps"],
        "tilt_deg": tilt,
        "body_rate_rad_s": distance(trial["final_state"]["omega_body_rad_s"], [0, 0, 0]),
        "reserve_kg": c["propellant_kg"],
        "latitude_deg": math.degrees(lat),
        "longitude_deg": math.degrees(math.atan2(ey, ex)),
    }


def site_distance(m, area):
    p, q = map(math.radians, (m["latitude_deg"], area["latitude_deg"]))
    delta = math.radians(m["longitude_deg"] - area["longitude_deg"])
    h = math.sin((p - q) / 2) ** 2 + math.cos(p) * math.cos(q) * math.sin(delta / 2) ** 2
    return 6378137.0 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def contact_reasons(m, envelope, *, wait_s=0.0):
    if m is None or any(type(x) not in (int, float) or not math.isfinite(x) for x in m.values()):
        return ["finite_contact_missing"]
    c = envelope["operations"]["contact"]
    reasons = [
        key
        for key, limit in [
            ("speed_mps", c["maximum_speed_mps"]),
            ("tilt_deg", c["maximum_tilt_deg"]),
            ("body_rate_rad_s", c["maximum_body_rate_rad_s"]),
        ]
        if m[key] > limit
    ]
    loss = max(0.0, wait_s) * envelope["operations"]["waiting"]["loss_upper_kg_per_s"]
    if m["reserve_kg"] - loss < c["minimum_fuel_kg"]:
        reasons.append("propellant_reserve_after_loss_bound")
    return reasons


def wait_reasons(envelope, when, scheduled):
    w = envelope["operations"]["waiting"]
    delay = max(0.0, when - scheduled)
    reasons = []
    if when > scheduled + envelope["maximum_delay_s"]:
        reasons.append("original_deadline")
    if w["initial_energy_kwh"] - delay * w["power_kw"] / 3600 < w["minimum_energy_kwh"]:
        reasons.append("energy")
    if w["initial_temperature_k"] + delay * w["warming_k_per_s"] > w["maximum_temperature_k"]:
        reasons.append("temperature")
    return reasons


def fresh_match(candidate, observation, envelope):
    state = observation["state"]
    now = state["time_s"]
    samples = candidate["trials"][0]["coast"]
    left = [s for s in samples if s["time_s"] <= now + 1e-7]
    right = [s for s in samples if s["time_s"] >= now - 1e-7]
    if not left or not right:
        return ["state_outside_prediction_time"]
    a, b = left[-1], right[0]
    gap = b["time_s"] - a["time_s"]
    limits = envelope["operations"]["fresh_state_match"]
    if gap > limits["maximum_sample_gap_s"]:
        return ["prediction_coast_gap"]
    u = 0 if gap < 1e-8 else (now - a["time_s"]) / gap
    reasons = []
    for key, field in [
        ("r_eci_m", "position_m"),
        ("v_eci_mps", "velocity_mps"),
        ("omega_body_rad_s", "body_rate_rad_s"),
    ]:
        predicted = [x + u * (y - x) for x, y in zip(a[key], b[key])]
        if distance(predicted, state[key]) > limits[field]:
            reasons.append("fresh_" + field)
    q = [x + u * (y - x) for x, y in zip(a["q_body_to_eci"], b["q_body_to_eci"])]
    norm = math.sqrt(sum(x * x for x in q))
    dot = abs(sum(x * y for x, y in zip(q, state["q_body_to_eci"])) / norm)
    if math.degrees(2 * math.acos(min(1.0, dot))) > limits["attitude_deg"]:
        reasons.append("fresh_attitude")
    if (
        abs(
            a["propellant_kg"]
            + u * (b["propellant_kg"] - a["propellant_kg"])
            - state["propellant_kg"]
        )
        > limits["fuel_kg"]
    ):
        reasons.append("fresh_fuel")
    return reasons


def admit(candidate, envelope, observation, notice, scheduled):
    """Re-run at selection AND dispatch, using current observations and notice."""
    now = observation["state"]["time_s"]
    when = candidate["return_time_s"]
    reasons = wait_reasons(envelope, when, scheduled)
    if not now <= when + 2.001 <= scheduled + envelope["maximum_delay_s"] + 2.001:
        reasons.append("request_time")
    if (
        len(candidate["trials"]) != 4
        or candidate["signs"] != [0, -1, 1]
        or [t.get("integration_scale") for t in candidate["trials"]] != [1.0, 1.0, 1.0, 0.5]
    ):
        reasons.append("uncertainty_or_refinement_trials_missing")
    if candidate["envelope_sha256"] != checksum(envelope) or candidate["scheduled_s"] != scheduled:
        reasons.append("forecast_authority_binding")
    if not notice["issued_at_s"] <= now <= notice["expires_at_s"]:
        reasons.append("notice_stale")
    if candidate.get("notice_sequence") != notice.get("sequence"):
        reasons.append("forecast_notice_binding")
    reasons += fresh_match(candidate, observation, envelope)
    if envelope.get("event_supervision"):
        observed_engines = observation["state"]["engine_states"]
        original_engines = candidate["trials"][0]["origin"]["state"]["engine_states"]
        if (
            [e["available"] for e in observed_engines] != [e["available"] for e in original_engines]
            or not all(e["available"] for e in observed_engines)
            or distance(observation["state"]["omega_body_rad_s"], [0, 0, 0])
            > envelope["event_supervision"]["maximum_coast_body_rate_rad_s"]
        ):
            reasons.append("observed_health_outside_forecast_domain")
    matched = []
    for trial in candidate["trials"]:
        m = metrics(trial)
        reasons += contact_reasons(m, envelope, wait_s=when - scheduled)
        if m is None:
            continue
        pad = envelope["operations"]["uncertainty"]
        sites = [
            name
            for name, area in envelope["operations"]["areas"].items()
            if site_distance(m, area) + pad["contact_footprint_padding_m"] <= area["radius_m"]
            and name in notice["areas"]
            and notice["areas"][name][0] <= m["time_s"] - pad["contact_time_padding_s"]
            and m["time_s"] + pad["contact_time_padding_s"] <= notice["areas"][name][1]
            and m["time_s"] <= notice["expires_at_s"]
        ]
        matched.append(set(sites))
    common = set.intersection(*matched) if len(matched) == 4 else set()
    if not common:
        reasons.append("area_or_availability")
    return {
        "accepted": not reasons,
        "reasons": sorted(set(reasons)),
        "areas": sorted(common),
        "time_s": now,
        "candidate_sha256": checksum(candidate),
        "observation_sha256": checksum(observation),
        "notice_sha256": checksum(notice),
        "envelope_sha256": checksum(envelope),
    }


def case_reasons(study, expected_case, outcome):
    """Completion uses the dispatched revision, never an abandoned AI choice."""
    reasons = []
    if outcome != "contact_and_recovery_area_met":
        reasons.append("contact_and_area_not_met")
    dispatch = study["dispatch"]
    selected = None if dispatch is None else dispatch["candidate_id"]
    executed = next(
        (
            r
            for r in study["plan_revisions"]
            if dispatch and r["revision"] == dispatch["revision"] and r["candidate_id"] == selected
        ),
        None,
    )
    if expected_case in ("normal", "event_normal") and selected != "nominal":
        reasons.append("normal_return_changed")
    if expected_case in ("recovery_update", "event_tradeoff"):
        record = next(
            (
                r
                for r in study["decisions"]
                if executed and r["request"]["request_id"] == executed["decision_request_id"]
            ),
            None,
        )
        if (
            (
                selected != "next_orbit"
                if expected_case == "recovery_update"
                else selected not in ("nominal", "next_orbit")
            )
            or not executed
            or executed["basis"] != "ai"
            or not record
            or record["accepted_action"] != "select_" + str(selected)
            or not (record["response"] or {}).get("model_inference_invoked")
        ):
            reasons.append("actual_ai_replanning_not_executed")
    if expected_case == "event_tradeoff" and executed:
        decision = next(
            (
                r
                for r in study["decisions"]
                if r["request"]["request_id"] == executed["decision_request_id"]
            ),
            None,
        )
        if (
            not decision
            or decision["request"]["notice"]["sequence"] < 2
            or not all(
                "select_" + n in decision["request"]["allowed_actions"]
                for n in ("nominal", "next_orbit")
            )
            or any(
                decision["request"]["candidates"][n]["constraint_rejections"]
                for n in ("nominal", "next_orbit")
            )
        ):
            reasons.append("two_admissible_options_after_correction_not_demonstrated")
    if expected_case in ("decision_timeout", "event_timeout"):
        # The registered scenario starts loss at this observed update, not at
        # a potentially incomplete set of self-reported injection flags.
        start = next(
            (
                i
                for i, r in enumerate(study["decisions"])
                if r["request"]["stage"] == "updated_recovery_status"
                or (expected_case == "event_timeout" and r["request"]["notice"]["sequence"] >= 2)
            ),
            None,
        )
        timed = [] if start is None else study["decisions"][start:]
        if (
            not timed
            or any(r["response"] is not None or not r["synthetic_timeout_injected"] for r in timed)
            or not executed
            or executed["basis"] != "checked_fallback"
        ):
            reasons.append("persistent_timeout_and_fallback_missing")
    return reasons


def decision_budget_reasons(records, maximum):
    reasons, used = [], 0
    for r in records:
        disposition = r["request_disposition"]
        if disposition == "budget_exhausted":
            if used < maximum or r["response"] is not None or r["accepted_action"] is not None:
                reasons.append("invalid_budget_exhaustion")
        elif disposition in ("submitted", "synthetic_outage"):
            used += 1
        else:
            reasons.append("unknown_request_disposition")
    if used > maximum:
        reasons.append("model_budget")
    return reasons


def verify(study, *, expected_envelope, expected_case, expected_sources):
    reasons = []
    try:
        if (
            study["envelope"] != expected_envelope
            or study["case"] != expected_case
            or study["source_sha256"] != expected_sources
        ):
            reasons.append("approved_input_binding")
        envelope = expected_envelope
        if (
            study["launch"]["outcome"]["payload_released_count"] != 26
            or not study["launch"]["outcome"]["orbit_gate_reached"]
        ):
            reasons.append("launch_and_deployment")
        if study["launch"]["final_state"] != study["execution_origin"]:
            reasons.append("launch_continuation")
        for candidate in study["candidates"].values():
            if candidate["source_sha256"] != expected_sources:
                reasons.append("forecast_source_binding")
            for trial in candidate["trials"]:
                if trial["origin"]["basis"] != "synthetic_navigation_estimate_v1":
                    reasons.append("forecast_observation_boundary")
        for index, revision in enumerate(study["plan_revisions"]):
            candidate = revision["candidate_snapshot"]
            checked = admit(
                candidate,
                envelope,
                revision["observation"],
                revision["notice"],
                study["scheduled_s"],
            )
            if (
                revision["revision"] != index + 1
                or revision["candidate_sha256"] != checksum(candidate)
                or revision["check"] != checked
                or not checked["accepted"]
            ):
                reasons.append("plan_revision_admission")
        if len(study["plan_revisions"]) > envelope["maximum_plan_revisions"]:
            reasons.append("revision_budget")
        # Existing scalar trajectory and rigid-body material-contact checks;
        # no propagator/controller imports and no trust in a success flag.
        from .starship_sixdof_verifier import (
            verify_study,
            _samples,
            _contact,
            _same_state,
            _Invalid,
        )

        launch_check = verify_study(
            {
                "schema": "missionos.starship_sixdof_study.v1",
                "profile": study["profile"],
                "runs": [study["launch"]],
                "provenance": {
                    "physical_execution_invoked": False,
                    "starship_vehicle_validated": False,
                },
            },
            expected_scenario="launch",
        )
        if not launch_check["passed"]:
            reasons.append("launch_trajectory_verification")
        try:
            first, last = _samples(
                study["execution"]["samples"],
                "$.execution.samples",
                [0],
                "ship",
                wind_profile=study["profile"],
            )
            if not _same_state(first, study["execution_origin"]) or not _same_state(
                last, study["execution"]["final_state"]
            ):
                reasons.append("continuation_state_binding")
            receipt = study["execution"]["outcome"]["contact_receipt"]
            if receipt is not None:
                g = study["profile"]["geometry"]
                _contact(receipt, last, "$.execution.contact", g["ship_length_m"], g["radius_m"])
        except _Invalid:
            reasons.append("execution_trajectory_or_contact_invalid")
        ids = set()
        for record in study["decisions"]:
            request = record["request"]
            response = record["response"]
            if request["request_id"] in ids or request["envelope_sha256"] != checksum(envelope):
                reasons.append("decision_identity")
            ids.add(request["request_id"])
            if request["deadline_s"] != request["time_s"] + envelope["decision_timeout_s"]:
                reasons.append("decision_deadline")
            if record["accepted_action"] is not None:
                if (
                    response is None
                    or record["accepted_action"] not in request["allowed_actions"]
                    or record["accepted_action"] != response.get("action")
                    or record["later_time_s"] > request["deadline_s"]
                ):
                    reasons.append("unapproved_decision")
            if record["later_observation"]["state"]["time_s"] != record["later_time_s"]:
                reasons.append("decision_observation_time")
            if response is not None and (
                response.get("request_sha256") != checksum(request)
                or response.get("request_id") != request["request_id"]
            ):
                reasons.append("response_binding")
            if record["later_time_s"] <= request["time_s"]:
                reasons.append("later_observation")
        records = {r["request"]["request_id"]: r for r in study["decisions"]}
        for revision in study["plan_revisions"]:
            decision = records.get(revision["decision_request_id"])
            if decision is None:
                reasons.append("revision_decision_missing")
                continue
            chosen = "select_" + revision["candidate_id"]
            if revision["basis"] == "checked_fallback":
                if decision["accepted_action"] is not None:
                    reasons.append("fallback_falsely_attributed")
            elif decision["accepted_action"] != chosen:
                reasons.append("revision_action_binding")
            elif revision["basis"] == "ai" and not (decision["response"] or {}).get(
                "model_inference_invoked"
            ):
                reasons.append("ai_inference_missing")
        reasons.extend(decision_budget_reasons(study["decisions"], envelope["maximum_jev_calls"]))
        if study["forecast_count"] > envelope["maximum_forecasts"]:
            reasons.append("forecast_budget")
        for operation in study["pending_operations"]:
            if (
                operation["end_time_s"] - operation["start_time_s"] + 1e-6
                < operation["wall_time_s"]
            ):
                reasons.append("clock_frozen_during_computation")
        dispatch = study["dispatch"]
        if dispatch is None:
            outcome = "unresolved"
        else:
            candidate = study["candidates"][dispatch["candidate_id"]]
            if (
                not study["plan_revisions"]
                or dispatch["revision"] != study["plan_revisions"][-1]["revision"]
                or dispatch["candidate_id"] != study["plan_revisions"][-1]["candidate_id"]
            ):
                reasons.append("dispatch_revision_binding")
            check = admit(
                candidate,
                envelope,
                dispatch["observation"],
                dispatch["notice"],
                study["scheduled_s"],
            )
            if check != dispatch["check"] or not check["accepted"]:
                reasons.append("dispatch_admission")
            if (
                abs(
                    study["execution"]["return_controller"]["return_time_s"]
                    - candidate["return_time_s"]
                )
                > 1e-6
            ):
                reasons.append("executed_return_binding")
            events = [e for e in study["execution"]["events"] if e["event"] == "return_requested"]
            if len(events) != 1 or abs(events[0]["time_s"] - dispatch["time_s"]) > 1e-6:
                reasons.append("return_command_observation")
            m = metrics(study["execution"])
            contact = contact_reasons(
                m, envelope, wait_s=candidate["return_time_s"] - study["scheduled_s"]
            )
            actual_areas = [
                name
                for name in check["areas"]
                if m
                and site_distance(m, envelope["operations"]["areas"][name])
                <= envelope["operations"]["areas"][name]["radius_m"]
                and dispatch["notice"]["areas"][name][0]
                <= m["time_s"]
                <= dispatch["notice"]["areas"][name][1]
            ]
            outcome = (
                "contact_and_recovery_area_met"
                if not contact and actual_areas
                else "contact_or_area_failed"
            )
        if envelope.get("event_supervision"):
            from .starship_replanning_events import verify_events

            reasons.extend(verify_events(study, envelope))
        completion_reasons = case_reasons(study, expected_case, outcome)
        return {
            "passed": not reasons,
            "issues": sorted(set(reasons)),
            "outcome": outcome,
            "case_accepted": not reasons and not completion_reasons,
            "case_issues": completion_reasons,
            "mission_completed": False,
            "physical_execution": False,
            "independent_physics_validation": False,
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError) as error:
        return {
            "passed": False,
            "issues": ["invalid_record:" + type(error).__name__],
            "outcome": "unresolved",
            "mission_completed": False,
            "physical_execution": False,
        }

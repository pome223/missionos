"""Scalar M1 constraint/result checks, independent of the GNC and propagator.

These checks establish recorded constraints in a declared synthetic model, not
physical-model accuracy. No successful forecast flag or controller status is
accepted as a contact measurement.
"""

from hashlib import sha256
import json
import math
from pathlib import Path

from .starship_call_limits import call_count_allowed, has_call_slot, valid_call_limit


def _expected_authority_gap_deadline(envelope, request):
    """Independently check authority-gap evidence, never infer an action.

    The composed inflight audit additionally binds this public authority and its
    remaining wall budget to the frozen contract and exact executor snapshot.
    No provider or executor eligibility helper is used here.
    """
    if "authority_gap_reasoning" not in envelope:
        return None
    try:
        authority, context = request["active_authority"], request["comparison_context"]
        if type(authority) is not dict or type(context) is not dict:
            return None
        generation, scope = context.get("generation_sha256"), authority.get("scope")
        if (type(generation) is not str or len(generation) != 64
                or any(c not in "0123456789abcdef" for c in generation)
                or context.get("missing_candidate_ids") != []
                or type(context.get("prepared_candidate_ids")) is not list
                or "next_orbit" not in context["prepared_candidate_ids"]
                or authority.get("schema") != "missionos.inflight_active_authority.v2"
                or type(authority.get("scope_revision")) is not int
                or authority["scope_revision"] != 0
                or authority.get("amendment_status") != "idle"
                or authority.get("select_next_orbit_requires_candidate_bound_operator_approval") is not True
                or authority.get("technical_candidates_are_not_authorization") is not True
                or type(scope) is not dict or type(scope.get("commands")) is not list
                or authority.get("scope_sha256") != checksum(scope)):
            return None
        commands, candidates = scope["commands"], request["candidates"]
        if ("select_next_orbit" in commands or "select_next_orbit" not in request["allowed_actions"]
                or any(type(name) is not str or type(candidate) is not dict
                       for name, candidate in candidates.items())
                or any(candidate.get("constraint_rejections") == [] and "select_" + name in commands
                       for name, candidate in candidates.items())):
            return None
        candidate = candidates["next_orbit"]
        material = candidate.get("comparison_material")
        if (candidate.get("constraint_rejections") != [] or type(material) is not dict
                or material.get("candidate_sha256") != candidate["candidate_sha256"]
                or material.get("notice_sha256") != checksum(request["notice"])
                or material.get("return_time_s") != candidate["return_time_s"]):
            return None
        window, now = authority["request_window"], request["time_s"]
        numeric = ("simulation_deadline_s", "remaining_simulation_s", "remaining_wall_s",
                   "model_response_reserve_s", "operator_response_reserve_s", "coast_guard_s",
                   "latest_decision_start_s")
        lead = 150.0 + 20.0 + 4.002
        if (type(window) is not dict
                or window.get("schema") != "missionos.inflight_request_window.v1"
                or any(type(window.get(key)) not in (int, float) or not math.isfinite(window[key])
                       for key in numeric)
                or window.get("request_available") is not True
                or window.get("request_attempted") is not False
                or window.get("trusted_telemetry_available") is not True
                or window.get("reason") != "request_window_open"
                or window["model_response_reserve_s"] != 150.0
                or window["operator_response_reserve_s"] != 20.0
                or window["coast_guard_s"] != 4.002
                or window["remaining_simulation_s"] != window["simulation_deadline_s"] - now
                or window["latest_decision_start_s"] != window["simulation_deadline_s"] - lead
                or min(window["remaining_simulation_s"], window["remaining_wall_s"]) < lead):
            return None
        return window["simulation_deadline_s"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _expected_planning_window(envelope, request):
    """Independently recompute the time grant from the recorded public inputs.

    Do not call the executor's decision-window selector or trust its claimed
    deadline. The separately approved fixed contract is checked in full.
    """
    expected_config = {
        "schema": "missionos.orbital_planning_window.v1",
        "scope": "prepared_uncertain_orbital_comparison",
        "provider_timeout_s": 120.0,
        "decision_timeout_s": 150.0,
        "booking_margin_s": 60.0,
        "coast_step_bound_s": 2.001,
    }
    result = {
        "schema": "missionos.orbital_planning_window_decision.v1",
        "duration_s": envelope["decision_timeout_s"],
        "provider_timeout_s": 30.0,
        "admitted": False,
        "reason": "legacy",
        "protected_until_s": None,
    }
    if "authority_gap_reasoning" in envelope:
        # Standalone canonical policy, independent of the producer's constant.
        expected_gap = {
            "schema": "missionos.authority_gap_reasoning.v1",
            "scope": "candidate_bound_scope_amendment",
            "operator_response_reserve_s": 20.0,
            "coast_guard_s": 4.002,
            "retry_policy": "once_per_prepared_generation",
        }
        if (checksum(envelope["authority_gap_reasoning"]) != checksum(expected_gap)
                or "orbital_planning_window" not in envelope
                or envelope.get("provider_budget_policy") != "independent_provider_reservations_v1"):
            raise ValueError("invalid_authority_gap_reasoning_contract")
    if "orbital_planning_window" not in envelope:
        return result
    if (
        checksum(envelope["orbital_planning_window"]) != checksum(expected_config)
        or envelope["decision_timeout_s"] != 35.0
        or not envelope.get("comparison_preparation")
        or envelope.get("event_supervision", {}).get("scope")
        != "post_deployment_orbital_coast_only"
    ):
        raise ValueError("invalid_orbital_planning_window_contract")
    result["reason"] = "invalid_comparison_inputs"
    if type(request) is not dict:
        return result
    try:

        def number(x):
            return type(x) in (int, float) and math.isfinite(x)

        now, notice = request["time_s"], request["notice"]
        if (
            not number(now)
            or not number(notice["expires_at_s"])
            or not number(notice["issued_at_s"])
            or not notice["issued_at_s"] <= now < notice["expires_at_s"]
        ):
            return result
        context = request.get("comparison_context")
        if type(context) is not dict or context.get("ready") is not True:
            result["reason"] = "comparison_not_ready"
            return result
        candidates, allowed = request["candidates"], request["allowed_actions"]
        if type(candidates) is not dict or type(allowed) is not list:
            return result
        prepared = [
            c
            for name, c in candidates.items()
            if type(name) is str
            and type(c) is dict
            and c.get("constraint_rejections") == []
            and type(c.get("comparison_material")) is dict
            and "select_" + name in allowed
        ]
        gap_deadline = _expected_authority_gap_deadline(envelope, request)
        if gap_deadline is not None:
            returns = [candidate["return_time_s"] for candidate in candidates.values()
                       if candidate.get("constraint_rejections") == []]
            if (not returns or not all(number(when) for when in returns)
                    or now + 150.0 + 60.0 + 2.001 > min(notice["expires_at_s"], *returns)):
                result["reason"] = "insufficient_booking_margin"
                return result
            result.update(duration_s=150.0, provider_timeout_s=120.0, admitted=True,
                          reason="authority_gap", protected_until_s=gap_deadline)
            return result
        if len(prepared) < 2:
            result["reason"] = "insufficient_admissible_candidates"
            return result
        service = notice.get("recovery_service_forecast", {})
        if type(service) is not dict or not any(
            type(v) is list and len(v) == 2 and all(number(x) for x in v) and v[0] < v[1]
            for v in service.values()
        ):
            result["reason"] = "comparison_not_uncertain"
            return result
        returns = [
            c["return_time_s"]
            for c in candidates.values()
            if type(c) is dict and c.get("constraint_rejections") == []
        ]
        if not returns or not all(number(t) for t in returns):
            return result
        protected = min([notice["expires_at_s"], *returns])
        result["protected_until_s"] = protected
        if now + 150.0 + 60.0 + 2.001 > protected:
            result["reason"] = "insufficient_booking_margin"
            return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return result
    result.update(
        duration_s=150.0, provider_timeout_s=120.0, admitted=True, reason="long_comparison_admitted"
    )
    return result


def planning_window_reasons(record, envelope, coast_dt):
    """Audit opt-in eligibility, the absolute decision deadline and provider cap."""
    try:
        request = record["request"]
        window = _expected_planning_window(envelope, request)
        if "orbital_planning_window" in envelope:
            if (
                type(coast_dt) not in (int, float)
                or not math.isfinite(coast_dt)
                or not 0 < coast_dt <= 2.001
                or checksum(request.get("planning_window")) != checksum(window)
            ):
                return ["orbital_planning_window_binding"]
        elif "planning_window" in request:
            return ["unapproved_orbital_planning_window"]
        if request["deadline_s"] != request["time_s"] + window["duration_s"]:
            return ["decision_deadline"]
        receipt = (record.get("response") or {}).get("llm_invocation") or {}
        if "orbital_planning_window" in envelope and receipt.get("call_attempted"):
            if receipt.get("timeout_seconds") != window["provider_timeout_s"]:
                return ["orbital_provider_timeout_binding"]
            if receipt.get("call_succeeded") is True:
                latency = receipt.get("latency_ms")
                if (
                    type(latency) not in (int, float)
                    or not math.isfinite(latency)
                    or not 0 <= latency <= window["provider_timeout_s"] * 1000
                ):
                    return ["orbital_provider_deadline"]
        return []
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["orbital_planning_window_binding"]


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


def admit(candidate, envelope, observation, notice, scheduled, *, expected_deployment_binding=None):
    """Re-run at selection AND dispatch, using current observations and notice."""
    now = observation["state"]["time_s"]
    when = candidate["return_time_s"]
    reasons = wait_reasons(envelope, when, scheduled)
    if expected_deployment_binding is not None:
        if (
            checksum(candidate.get("deployment_binding")) != checksum(expected_deployment_binding)
            or type(observation.get("retained_count")) is not int
            or observation["retained_count"] != expected_deployment_binding["retained_count"]
            or any(
                type(t["origin"].get("retained_count")) is not int
                or t["origin"]["retained_count"] != observation["retained_count"]
                for t in candidate["trials"]
            )
        ):
            reasons.append("deployment_inventory_or_generation_binding")
    elif "deployment_binding" in candidate:
        reasons.append("unexpected_deployment_binding")
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
    # Orbital predictions do not depend on recovery-service text or clearances.
    # Event supervision rechecks the current notice below without discarding
    # otherwise valid dynamics. Preserve the older M1 admission contract.
    if not envelope.get("event_supervision") and candidate.get("notice_sequence") != notice.get(
        "sequence"
    ):
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


def event_tradeoff_witnesses(study, executed, *, expected_deployment_binding=None):
    """Identify live decisions that control the exact dispatched booking.

    Keeping an unchanged booking need not create a revision. A witness still
    binds an actual response to stored forecast snapshots and rechecks current
    notice, state and constraints. Requests record candidate checks, but not a
    full request-time navigation observation; only the later observation can
    be independently re-admitted here. Missing forecasts are never inferred.
    """
    if not executed or not study.get("dispatch"):
        return []
    dispatch, envelope = study["dispatch"], study["envelope"]
    selected = executed["candidate_id"]
    candidate_hash = checksum(executed["candidate_snapshot"])
    if (
        selected not in ("nominal", "next_orbit")
        or executed["candidate_sha256"] != candidate_hash
        or dispatch["revision"] != executed["revision"]
        or dispatch["candidate_id"] != selected
        or checksum(study["candidates"].get(selected)) != candidate_hash
    ):
        return []
    versions = {}
    for name, candidate in study["candidates"].items():
        versions[name, checksum(candidate)] = candidate
    for revision in study["plan_revisions"]:
        candidate = revision["candidate_snapshot"]
        versions[revision["candidate_id"], checksum(candidate)] = candidate
    witnesses = []
    for record in study["decisions"]:
        request, response = record["request"], record["response"] or {}
        action = record["accepted_action"]
        reaffirmed = (
            request.get("selected") == selected
            and request["revision"] == executed["revision"]
            and executed["time_s"] <= request["time_s"]
        )
        created = (
            executed["decision_request_id"] == request["request_id"]
            and executed["revision"] == request["revision"] + 1
            and executed["time_s"] == record["later_time_s"]
            and action == "select_" + selected
            and executed["basis"] == "ai"
        )
        if (
            not (reaffirmed or created)
            or action not in ("select_" + selected, "keep_plan")
            or (action == "keep_plan" and not reaffirmed)
            or action not in request["allowed_actions"]
            or response.get("action") != action
            or response.get("request_id") != request["request_id"]
            or response.get("request_sha256") != checksum(request)
            or response.get("model_inference_invoked") is not True
            or response.get("mode") != "live"
            or record.get("fallback") is not False
            or record.get("interrupted_by_events", False)
            or request["notice"]["sequence"] < 2
            or not request["time_s"] < record["later_time_s"] <= dispatch["time_s"]
            or record["later_time_s"] > request["deadline_s"]
            or record["later_observation"]["state"]["time_s"] != record["later_time_s"]
        ):
            continue
        admitted = {}
        hashes = {}
        for name in ("nominal", "next_orbit"):
            public = request["candidates"].get(name)
            candidate = None if not public else versions.get((name, public.get("candidate_sha256")))
            if (
                candidate is None
                or public["constraint_rejections"]
                or public["return_time_s"] != candidate["return_time_s"]
                or checksum(public["predicted_contacts"])
                != checksum([metrics(t) for t in candidate["trials"]])
                or candidate["trials"][0]["origin"]["state"]["time_s"] > request["time_s"]
            ):
                continue
            check = admit(
                candidate,
                envelope,
                record["later_observation"],
                request["notice"],
                study["scheduled_s"],
                expected_deployment_binding=expected_deployment_binding,
            )
            if check["accepted"]:
                admitted[name] = check
                hashes[name] = checksum(candidate)
        if hashes.get(selected) != candidate_hash:
            continue
        both = all(
            name in admitted and "select_" + name in request["allowed_actions"]
            for name in ("nominal", "next_orbit")
        )
        witnesses.append(
            {
                "request_id": request["request_id"],
                "accepted_action": action,
                "executed_revision": executed["revision"],
                "candidate_id": selected,
                "candidate_sha256": candidate_hash,
                "request_time_s": request["time_s"],
                "later_time_s": record["later_time_s"],
                "notice_sequence": request["notice"]["sequence"],
                "existing_booking_reaffirmed": reaffirmed,
                "request_navigation_observation_rechecked": False,
                "request_checks_basis": "recorded_candidate_hashes_and_constraint_results",
                "later_admission_checks": admitted,
                "both_options_admissible": both,
            }
        )
    return witnesses


def event_operation_notice_reasons(study, expected_case):
    """Bind operational notices to registered input at each actual action time."""
    from .starship_replanning import notices

    reasons = []
    operations = [("revision", r) for r in study["plan_revisions"]]
    if study["dispatch"] is not None:
        operations.append(("dispatch", study["dispatch"]))
    for kind, record in operations:
        when, observed = record["time_s"], record["observation"]["state"]["time_s"]
        finite = all(type(t) in (int, float) and math.isfinite(t) for t in (when, observed))
        if not finite or when != observed:
            reasons.append(kind + "_observation_time")
        if finite and checksum(record["notice"]) != checksum(notices(expected_case, when)):
            reasons.append(kind + "_notice_registration")
    return reasons


def case_reasons(study, expected_case, outcome, *, expected_deployment_binding=None):
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
    if expected_case in ("normal", "event_normal", "comparison_normal") and selected != "nominal":
        reasons.append("normal_return_changed")
    if expected_case == "comparison_normal":
        preparations = study["comparison_preparations"]
        if (
            type(study["forecast_count"]) is not int
            or study["forecast_count"] != 4
            or len(preparations) != 1
            or preparations[0]["initial"] is not True
            or preparations[0]["requested_candidate_ids"] != ["nominal"]
            or preparations[0]["forecast_count_before"] != 0
            or preparations[0]["forecast_count_after"] != 4
        ):
            reasons.append("normal_optional_computation_not_avoided")
    if expected_case == "recovery_update":
        record = next(
            (
                r
                for r in study["decisions"]
                if executed and r["request"]["request_id"] == executed["decision_request_id"]
            ),
            None,
        )
        if (
            selected != "next_orbit"
            or not executed
            or executed["basis"] != "ai"
            or not record
            or record["accepted_action"] != "select_" + str(selected)
            or not (record["response"] or {}).get("model_inference_invoked")
        ):
            reasons.append("actual_ai_replanning_not_executed")
    if expected_case == "event_tradeoff":
        witnesses = event_tradeoff_witnesses(
            study, executed, expected_deployment_binding=expected_deployment_binding
        )
        if not witnesses:
            reasons.append("actual_ai_replanning_not_executed")
        if not any(w["both_options_admissible"] for w in witnesses):
            reasons.append("two_admissible_options_after_correction_not_demonstrated")
    if expected_case in ("decision_timeout", "event_timeout", "comparison_timeout"):
        # The registered scenario starts loss at this observed update, not at
        # a potentially incomplete set of self-reported injection flags.
        start = next(
            (
                i
                for i, r in enumerate(study["decisions"])
                if r["request"]["stage"] == "updated_recovery_status"
                or (
                    expected_case in ("event_timeout", "comparison_timeout")
                    and r["request"]["notice"]["sequence"] >= 2
                )
            ),
            None,
        )
        timed = [] if start is None else study["decisions"][start:]
        checked_fallback = bool(executed and executed["basis"] == "checked_fallback")
        if (
            expected_case in ("event_timeout", "comparison_timeout")
            and executed
            and not checked_fallback
        ):
            # Keeping a valid booking does not spend a new plan revision. The
            # observed outage response must still freshly admit that exact
            # booked candidate, and the independently checked dispatch must use it.
            for record in timed:
                request = record["request"]
                if (
                    record.get("fallback") is not True
                    or record.get("interrupted_by_events", False)
                    or record["accepted_action"] is not None
                    or request.get("selected") != executed["candidate_id"]
                    or request["revision"] != executed["revision"]
                    or not executed["time_s"]
                    <= request["time_s"]
                    <= record["later_time_s"]
                    <= dispatch["time_s"]
                ):
                    continue
                check = admit(
                    executed["candidate_snapshot"],
                    study["envelope"],
                    record["later_observation"],
                    request["notice"],
                    study["scheduled_s"],
                    expected_deployment_binding=expected_deployment_binding,
                )
                if check["accepted"]:
                    checked_fallback = True
                    break
        if (
            not timed
            or any(r["response"] is not None or not r["synthetic_timeout_injected"] for r in timed)
            or not checked_fallback
        ):
            reasons.append("persistent_timeout_and_fallback_missing")
    if expected_case in ("comparison_update", "comparison_orbital_update"):
        comparisons = []
        for record in study["decisions"]:
            request = record["request"]
            response = record["response"] or {}
            if (
                request["notice"]["sequence"] < 2
                or not request.get("comparison_context", {}).get("ready")
                or record["accepted_action"]
                not in ("keep_plan", "select_nominal", "select_next_orbit")
                or record.get("fallback")
                or record.get("interrupted_by_events")
                or (
                    study["envelope"]["mode"] == "live"
                    and not response.get("model_inference_invoked")
                )
            ):
                continue
            chosen = (
                record["accepted_action"].removeprefix("select_")
                if record["accepted_action"].startswith("select_")
                else request.get("selected")
            )
            bound_to_dispatch = bool(
                executed
                and chosen == selected
                and (
                    executed["decision_request_id"] == request["request_id"]
                    or (
                        request["revision"] == executed["revision"]
                        and request.get("selected") == selected
                        and executed["time_s"] <= request["time_s"]
                    )
                )
            )
            if not bound_to_dispatch:
                continue
            if study["envelope"]["mode"] == "live":
                basis = response.get("decision_basis") or {}
                if response.get("routing", {}).get("route") == "prepared_uncertain_comparison" and (
                    type(basis) is not dict
                    or set(basis.get("compared_candidate_ids", [])) != {"nominal", "next_orbit"}
                    or not basis.get("summary")
                ):
                    continue
            if expected_case == "comparison_orbital_update" and study["envelope"]["mode"] == "live":
                from .starship_reference_ids import verify_reference_ids

                basis = response.get("decision_basis") or {}
                llm = response.get("llm_invocation") or {}
                if (
                    response.get("routing", {}).get("route") != "prepared_uncertain_comparison"
                    or not request.get("planning_window", {}).get("admitted")
                    or llm.get("call_attempted") is not True
                    or llm.get("call_succeeded") is not True
                    or llm.get("model_inference_invoked") is not True
                    or verify_reference_ids(
                        request, basis.get("reference_ids"), basis.get("compared_candidate_ids")
                    )["status"]
                    != "valid"
                ):
                    continue
            if all(
                name in request["candidates"]
                and not request["candidates"][name]["constraint_rejections"]
                and "select_" + name in request["allowed_actions"]
                for name in ("nominal", "next_orbit")
            ):
                comparisons.append(record)
        if not comparisons:
            reasons.append("prepared_comparison_decision_not_observed")
    if expected_case in ("comparison_late", "comparison_tool_failure"):
        wanted = (
            "booking_or_candidate_deadline"
            if expected_case == "comparison_late"
            else "synthetic_tool_cancellation"
        )
        matched = [
            row
            for row in study["comparison_preparations"]
            if row["notice"]["sequence"] >= 2
            and row["reason"] == wanted
            and row["selected_before"] is not None
            and row["selected_before"] == row["selected_after"]
        ]
        if not matched or selected != "nominal":
            reasons.append("comparison_existing_booking_preservation_not_observed")
    return reasons


def decision_budget_reasons(records, maximum, *, envelope=None):
    if envelope is not None and "provider_budget_policy" in envelope:
        if (envelope["provider_budget_policy"] != "independent_provider_reservations_v1"
                or "orbital_planning_window" not in envelope):
            return ["invalid_provider_budget_policy"]
        return orbital_provider_budget_reasons(records, envelope)
    from .starship_call_limits import canonical_uncapped_jev_escalations
    if (type(maximum) is not int or maximum < 0
            or envelope is not None and envelope.get("maximum_llm_calls") is None
            and not canonical_uncapped_jev_escalations(envelope)):
        return ["invalid_provider_budget_policy"]
    reasons, used = [], 0
    for r in records:
        if "provider_budget" in r or "provider_budget" in r.get("request", {}):
            reasons.append("unapproved_provider_reservation")
        disposition = r["request_disposition"]
        if disposition == "budget_exhausted":
            if used < maximum or r["response"] is not None or r["accepted_action"] is not None:
                reasons.append("invalid_budget_exhaustion")
        elif disposition == "deadline_fallback":
            if (
                r["response"] is not None
                or r["accepted_action"] is not None
                or r.get("fallback") is not True
            ):
                reasons.append("invalid_deadline_fallback")
        elif disposition in ("submitted", "synthetic_outage"):
            used += 1
        else:
            reasons.append("unknown_request_disposition")
    if used > maximum:
        reasons.append("model_budget")
    return reasons


def orbital_provider_budget_reasons(records, envelope):
    """Recompute provider reservations independently from decision-time evidence.

    Reserved slots bound attempted delivery, including lost/invalid responses;
    they are not claimed API invocations. Actual calls are checked in host ledgers.
    """
    if (envelope.get("provider_budget_policy") != "independent_provider_reservations_v1"
            or "orbital_planning_window" not in envelope):
        return ["invalid_provider_budget_policy"]
    jev_limit, llm_limit = envelope.get("maximum_jev_calls"), envelope.get("maximum_llm_calls")
    if ("maximum_llm_calls" not in envelope
            or type(jev_limit) is not int or jev_limit < 0 or not valid_call_limit(llm_limit)
            or llm_limit is None and envelope.get("mode") != "live"):
        return ["invalid_provider_budget_policy"]
    reasons, used, reasoned, reserved = [], {"jev": 0, "deepseek": 0}, set(), []
    for row in records:
        try:
            request = row["request"]
            context = request.get("comparison_context") or {}
            generation = context.get("generation_sha256")
            comparison = envelope.get("comparison_preparation") or {}
            prepared = [
                candidate for name, candidate in request["candidates"].items()
                if candidate.get("constraint_rejections") == []
                and candidate.get("comparison_material")
                and "select_" + name in request["allowed_actions"]
            ]
            ranges = request["notice"].get("recovery_service_forecast", {}).values()
            uncertain = any(
                type(v) is list and len(v) == 2
                and all(type(x) in (int, float) and math.isfinite(x) for x in v)
                and v[0] < v[1] for v in ranges
            )
            window = _expected_planning_window(envelope, request)
            gap = window["admitted"] and window["reason"] == "authority_gap"
            deep = bool(comparison.get("deep_reasoning") and context.get("ready") is True
                        and generation and generation not in reasoned and window["admitted"]
                        and (gap or len(prepared) >= 2 and uncertain))
            route = ("authority_gap" if gap else "prepared_uncertain_comparison") if deep else "jev"
            provider = "deepseek" if deep else "jev"
            maximum = envelope["maximum_llm_calls" if deep else "maximum_jev_calls"]
            expected = {"provider": provider, "used_slots": used[provider],
                        "maximum_slots": maximum,
                        "generation_sha256": generation if deep else None,
                        "previous_reservations_sha256": checksum(reserved)}
            actual = row.get("provider_budget")
            if (type(actual) is not dict or checksum(actual) != checksum(expected)
                    or checksum(request.get("provider_budget")) != checksum(expected)
                    or request.get("envelope_sha256") != checksum(envelope)
                    or type(actual.get("used_slots")) is not int
                    or not valid_call_limit(actual.get("maximum_slots"))
                    or actual.get("maximum_slots") is None and provider != "deepseek"):
                reasons.append("provider_reservation_binding")
            response = row.get("response")
            if response is not None and envelope["mode"] == "live":
                wrong = response.get("jev_invocation" if deep else "llm_invocation") or {}
                if (wrong.get("call_attempted") is True
                        or response.get("routing", {}).get("route") != route
                        or response.get("request_sha256") != checksum(request)):
                    reasons.append("provider_response_reservation")
            disposition = row["request_disposition"]
            if disposition == "budget_exhausted":
                if (has_call_slot(used[provider], maximum) or row["response"] is not None
                        or row["accepted_action"] is not None):
                    reasons.append("invalid_budget_exhaustion")
            elif disposition == "deadline_fallback":
                if (row["response"] is not None or row["accepted_action"] is not None
                        or row.get("fallback") is not True):
                    reasons.append("invalid_deadline_fallback")
            elif disposition in ("submitted", "synthetic_outage"):
                used[provider] += 1
                reserved.append({"request_id": request["request_id"],
                                 "request_sha256": checksum(request)})
                if deep:
                    reasoned.add(generation)
                if not call_count_allowed(used[provider], maximum):
                    reasons.append("model_budget")
            else:
                reasons.append("unknown_request_disposition")
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            reasons.append("provider_reservation_binding")
    return reasons


def deadline_fallback_reasons(record, study, envelope, *, expected_deployment_binding=None):
    """Check a previously approved booking without spending a model wait.

    This exception is available only at the finite dispatch boundary. It cannot
    approve a new candidate, move a burn or hide an unrecorded model response.
    """
    request = record["request"]
    if record["request_disposition"] != "deadline_fallback":
        return ["unexpected_commitment_guard"] if "commitment_guard" in request else []
    try:
        guard = request["commitment_guard"]
        cfg = envelope["event_supervision"]
        dt = study["profile"]["integration"]["coast_dt_s"]
        now = request["time_s"]
        if (
            cfg.get("commitment_response")
            != "preserve_currently_admissible_booking_without_model_wait"
            or type(guard) is not dict
            or set(guard)
            != {"candidate_id", "candidate_sha256", "return_time_s", "guard_horizon_s"}
            or type(dt) not in (int, float)
            or not math.isfinite(dt)
            or dt <= 0
            or type(now) not in (int, float)
            or not math.isfinite(now)
            or guard["guard_horizon_s"] != envelope["decision_timeout_s"] + dt
            or not -min(dt, 2.001) <= guard["return_time_s"] - now <= guard["guard_horizon_s"]
            or request["selected"] != guard["candidate_id"]
            or record["response"] is not None
            or record["accepted_action"] is not None
            or record["fallback"] is not True
            or record.get("interrupted_by_events", False)
            or not 0 <= record["later_time_s"] - now <= dt
            or record["later_observation"]["state"]["time_s"] != record["later_time_s"]
        ):
            return ["invalid_deadline_fallback_guard"]
        booking = next(
            (
                r
                for r in study["plan_revisions"]
                if r["revision"] == request["revision"]
                and r["candidate_id"] == guard["candidate_id"]
            ),
            None,
        )
        if booking is None or booking["time_s"] > now:
            return ["deadline_fallback_booking_missing"]
        candidate = booking["candidate_snapshot"]
        if (
            checksum(candidate) != guard["candidate_sha256"]
            or candidate["return_time_s"] != guard["return_time_s"]
        ):
            return ["deadline_fallback_candidate_binding"]
        check = admit(
            candidate,
            envelope,
            record["later_observation"],
            request["notice"],
            study["scheduled_s"],
            expected_deployment_binding=expected_deployment_binding,
        )
        return [] if check["accepted"] else ["deadline_fallback_admission"]
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        return ["invalid_deadline_fallback_guard"]


def result_binding(
    study, *, expected_envelope, expected_case, expected_sources, expected_run_id=None
):
    """Identify exactly what was checked, including invalid JSON-shaped input.

    Missing or non-canonical data remains null with an explicit unavailable list;
    it is never replaced with a success-shaped digest of a different record.
    """
    unavailable = []

    def canonical(value, name):
        try:
            return checksum(value)
        except (TypeError, ValueError, OverflowError, RecursionError):
            unavailable.append(name)
            return None

    observed_run_id = study.get("run_id") if type(study) is dict else None
    if type(observed_run_id) is not str or not observed_run_id:
        observed_run_id = None
        unavailable.append("observed_run_id")
    try:
        verifier_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        verifier_hash = None
        unavailable.append("verifier_source_sha256")
    binding = {
        "schema": "missionos.starship_m1_verification.v2",
        "study_canonical_sha256": canonical(study, "study_canonical_sha256"),
        "expected_case": expected_case,
        "expected_run_id": expected_run_id,
        "observed_run_id": observed_run_id,
        "expected_sources_sha256": canonical(expected_sources, "expected_sources_sha256"),
        "expected_envelope_sha256": canonical(expected_envelope, "expected_envelope_sha256"),
        "verifier_source_sha256": verifier_hash,
    }
    binding["binding_unavailable"] = sorted(unavailable)
    binding["binding_complete"] = not unavailable
    return binding


def expected_comparison_material(candidate, envelope, notice, scheduled):
    """Independent accounting reconstruction; never import the material producer."""
    waiting = envelope["operations"]["waiting"]
    delay = max(0.0, candidate["return_time_s"] - scheduled)
    loss = delay * waiting["loss_upper_kg_per_s"]
    balances = {
        "elapsed_wait_s": delay,
        "energy_remaining_kwh": waiting["initial_energy_kwh"]
        - delay * waiting["power_kw"] / 3600.0,
        "temperature_upper_k": waiting["initial_temperature_k"]
        + delay * waiting["warming_k_per_s"],
        "propellant_loss_upper_kg": loss,
        "valid_until_s": scheduled + envelope["maximum_delay_s"],
    }
    contacts = [metrics(trial) for trial in candidate["trials"]]
    present = len(contacts) == 4 and all(contact is not None for contact in contacts)
    intervals, services = {}, {}
    if present:
        for key in ("time_s", "speed_mps", "tilt_deg", "body_rate_rad_s", "reserve_kg"):
            values = [contact[key] for contact in contacts]
            intervals[key] = [min(values), max(values)]
        padding = envelope["operations"]["uncertainty"]
        for area_id, area in envelope["operations"]["areas"].items():
            window = notice["areas"].get(area_id)
            compatible = window is not None and all(
                site_distance(contact, area) + padding["contact_footprint_padding_m"]
                <= area["radius_m"]
                and window[0] <= contact["time_s"] - padding["contact_time_padding_s"]
                and contact["time_s"] + padding["contact_time_padding_s"] <= window[1]
                and contact["time_s"] <= notice["expires_at_s"]
                for contact in contacts
            )
            if compatible:
                estimate = notice.get("recovery_service_forecast", {}).get(
                    area_id + "_delay_after_contact_s"
                )
                services[area_id] = {
                    "service_delay_after_contact_s": estimate,
                    "estimated_completion_time_s": (
                        [intervals["time_s"][0] + estimate[0], intervals["time_s"][1] + estimate[1]]
                        if estimate is not None
                        else None
                    ),
                }
    return {
        "schema": "missionos.m1_candidate_comparison.v1",
        "basis": "sampled_predictions_and_observed_notice_not_guaranteed_bounds",
        "candidate_sha256": checksum(candidate),
        "notice_sha256": checksum(notice),
        "return_time_s": candidate["return_time_s"],
        "waiting_at_burn": balances,
        "sampled_contact_ranges": intervals,
        "reserve_after_loss_kg": [x - loss for x in intervals["reserve_kg"]] if present else None,
        "area_service_estimates": services,
        "contact_samples_available": present,
        "limitations": [
            "four_sample_ranges_not_exhaustive_uncertainty_bounds",
            "waiting_accounted_until_burn_not_post_contact_service",
            "service_completion_is_uncertain_estimate_not_clearance_or_observed_outcome",
        ],
    }


def _comparison_objectives(material):
    """All dimensions expressed as intervals to minimize; no midpoint guesses."""
    services = material["area_service_estimates"]
    if len(services) != 1:
        return None
    completion = next(iter(services.values()))["estimated_completion_time_s"]
    fuel = material["reserve_after_loss_kg"]
    if completion is None or fuel is None:
        return None
    waiting = material["waiting_at_burn"]
    return {
        "estimated_completion_time_s": completion,
        "elapsed_wait_s": [waiting["elapsed_wait_s"]] * 2,
        "energy_remaining_kwh": [-waiting["energy_remaining_kwh"]] * 2,
        "temperature_upper_k": [waiting["temperature_upper_k"]] * 2,
        "reserve_after_loss_kg": [-fuel[1], -fuel[0]],
    }


def comparison_quality_record(record, materials, objectives):
    """Conservative post-hoc audit, never a command constraint or success gate."""
    from .starship_decision_evidence import verify_numeric_evidence
    from .starship_reference_ids import verify_reference_ids

    request, response = record["request"], record["response"] or {}
    action = record["accepted_action"]
    chosen = (
        action.removeprefix("select_")
        if action and action.startswith("select_")
        else request.get("selected")
        if action == "keep_plan"
        else None
    )
    vectors = {name: _comparison_objectives(material) for name, material in materials.items()}
    evidence = response.get("decision_basis") or {}
    if type(evidence) is not dict:
        evidence = {}
    row = {
        "request_id": request["request_id"],
        "chosen_candidate_id": chosen,
        "status": "indeterminate",
        "dominating_candidate_ids": [],
        "available_candidate_ids": sorted(materials),
        "comparison_explanation_present": bool(evidence.get("summary")),
        "reported_compared_candidate_ids": evidence.get("compared_candidate_ids", []),
        "numeric_evidence": verify_numeric_evidence(
            request,
            evidence.get("numeric_evidence"),
            compared_ids=evidence.get("compared_candidate_ids"),
        ),
        "internal_reasoning_verified": False,
    }
    if "reference_ids" in evidence:
        row["reference_id_evidence"] = verify_reference_ids(
            request,
            evidence["reference_ids"],
            evidence.get("compared_candidate_ids"),
        )
    # A missing/unknown input does not establish quality, even if one known
    # competitor looks worse. Only request-time admissible alternatives count.
    if chosen not in vectors or len(vectors) < 2 or any(v is None for v in vectors.values()):
        return row
    if not objectives or any(set(v) != set(objectives) for v in vectors.values()):
        return row
    selected = vectors[chosen]
    dominating = []
    for name, vector in vectors.items():
        if name == chosen:
            continue
        no_worse = all(vector[key][1] <= selected[key][0] for key in objectives)
        strictly_better = any(vector[key][1] < selected[key][0] for key in objectives)
        if no_worse and strictly_better:
            dominating.append(name)
    row.update(
        status="dominated" if dominating else "not_demonstrably_dominated",
        dominating_candidate_ids=sorted(dominating),
    )
    return row


def verify_comparison(study, envelope, expected_case, *, expected_deployment_binding=None):
    """Bind prepared evidence, decision-time costs and protected-booking budget."""
    from .starship_replanning import notices

    reasons, quality = [], []
    preparations = study["comparison_preparations"]
    rows, versions = {}, {}
    previous_count, previous_after, previous_end = 0, {}, -math.inf
    search = study.get("return_time_search")
    previous_operation_end = 0
    for index, row in enumerate(preparations):
        identity = row["preparation_id"]
        observed = row["observation"]
        start, end = row["start_time_s"], row["end_time_s"]
        times = dict(study["opportunities"])
        # A successful optional screen changes the later opportunity only
        # after this preparation's original budget has already been fixed.
        if search and search["accepted_for_trial"] and start <= search["start_time_s"]:
            times["next_orbit"] = search["center_s"]
        notice, budget = row["notice"], row["budget"]
        generation = checksum(
            {key: value for key, value in notice.items() if key not in ("sequence", "issued_at_s")}
        )
        if (
            identity in rows
            or type(row["initial"]) is not bool
            or type(row["work_performed"]) is not bool
            or any(
                type(t) not in (int, float) or not math.isfinite(t)
                for t in (start, end, row["observed_time_s"], observed["state"]["time_s"])
            )
            or row["schema"] != "missionos.m1_comparison_preparation.v1"
            or row["generation_sha256"] != generation
            or row["initial"] != (index == 0)
            or row["observed_time_s"] != start
            or observed["state"]["time_s"] != start
            or start > end
            or start < previous_end
            or checksum(notice) != checksum(notices(expected_case, start))
        ):
            reasons.append("comparison_preparation_context")
        rows[identity] = row
        previous_end = end
        for key, candidate in row["candidate_snapshots_after"].items():
            if key != checksum(candidate) or candidate["source_sha256"] != study["source_sha256"]:
                reasons.append("comparison_forecast_snapshot_binding")
            versions[key] = candidate
        before, after = row["candidate_sha256_before"], row["candidate_sha256_after"]
        if before != previous_after:
            reasons.append("comparison_unrecorded_forecast_change")
        previous_after = after
        if any(key not in versions for key in list(before.values()) + list(after.values())):
            reasons.append("comparison_forecast_snapshot_missing")
            continue
        config = envelope["comparison_preparation"]
        configured = config["initial_candidates"] if index == 0 else config["update_candidates"]
        requested = [name for name in configured if times[name] > start]
        reused = [
            name
            for name in requested
            if name in before and not fresh_match(versions[before[name]], observed, envelope)
        ]
        missing = [name for name in requested if name not in reused]
        if (
            row["requested_candidate_ids"] != requested
            or row["reused_candidate_ids"] != reused
            or row["missing_candidate_ids"] != missing
        ):
            reasons.append("comparison_reuse_or_request_binding")
        booking = row["selected_before"]
        protected = None
        if booking is not None:
            candidate = versions.get(booking["candidate_sha256"])
            if (
                candidate is None
                or before.get(booking["candidate_id"]) != booking["candidate_sha256"]
                or candidate["return_time_s"] != booking["return_time_s"]
            ):
                reasons.append("comparison_booking_binding")
            elif admit(
                candidate,
                envelope,
                observed,
                notice,
                study["scheduled_s"],
                expected_deployment_binding=expected_deployment_binding,
            )["accepted"]:
                protected = candidate["return_time_s"]
        dt = study["profile"]["integration"]["coast_dt_s"]
        margin = envelope["event_supervision"]["deadline_margin_s"]
        admissible = [
            name
            for name, key in before.items()
            if admit(
                versions[key],
                envelope,
                observed,
                notice,
                study["scheduled_s"],
                expected_deployment_binding=expected_deployment_binding,
            )["accepted"]
        ]
        limit = min(
            [times[name] for name in admissible]
            + [times[n] for n in missing]
            + [study["scheduled_s"] + envelope["maximum_delay_s"]]
        )
        reserved = (
            150.0 if "orbital_planning_window" in envelope else envelope["decision_timeout_s"]
        )
        opportunity_cutoff = limit - reserved - margin - dt
        available = max(0.0, min(envelope["batch_timeout_s"], opportunity_cutoff - start))
        cutoff = min(opportunity_cutoff, start + available)
        expected_budget = {
            "maximum_forecasts": envelope["maximum_forecasts"],
            "batch_timeout_s": envelope["batch_timeout_s"],
            "decision_timeout_s": reserved,
            "deadline_margin_s": margin,
            "coast_dt_s": dt,
            "protected_burn_s": protected,
            "admissible_candidate_ids": admissible,
            "opportunity_cutoff_time_s": opportunity_cutoff,
            "cutoff_time_s": cutoff,
            "available_sim_s": available,
        }
        if checksum(budget) != checksum(expected_budget):
            reasons.append("comparison_calculation_budget")
        count_before, count_after = row["forecast_count_before"], row["forecast_count_after"]
        operation_start = row["pending_operation_index"]
        operation_end = row.get(
            "pending_operation_end_index",
            operation_start + (1 if row["work_performed"] else 0)
            if type(operation_start) is int else None,
        )
        valid_indices = (
            type(operation_start) is int and type(operation_end) is int
            and previous_operation_end <= operation_start <= operation_end
            <= len(study["pending_operations"])
        )
        if (
            type(count_before) is not int
            or type(count_after) is not int
            or count_before != previous_count
            or count_after < count_before
            or count_after > envelope["maximum_forecasts"]
            or not valid_indices
        ):
            reasons.append("comparison_forecast_accounting")
        previous_count = count_after
        if valid_indices:
            if any(op["operation"] == "return_forecasts"
                   for op in study["pending_operations"][previous_operation_end:operation_start]):
                reasons.append("comparison_forecast_accounting")
            previous_operation_end = operation_end
        if row["work_performed"]:
            operations = (study["pending_operations"][operation_start:operation_end]
                          if valid_indices else [])
            screening = bool(search and search["start_time_s"] == start)
            screen_count = 0
            if screening:
                screen_count = search["forecast_count_after"] - search["forecast_count_before"]
                if (
                    search["forecast_count_before"] != count_before
                    or search["requested_names"] != missing
                    or type(screen_count) is not int or screen_count not in (0, 3)
                    or type(search["work_started"]) is not bool
                    or search["work_started"] != (screen_count == 3)
                    or not start <= search["end_time_s"] <= end
                ):
                    reasons.append("comparison_search_accounting")
            screen_operations = int(screen_count == 3)
            admission_operations = len(operations) - screen_operations
            admission_names = list(missing)
            admission_start = search["end_time_s"] if screening else start
            if search and search["end_time_s"] <= admission_start and search.get("search_cutoff_exceeded"):
                lead = available + reserved + margin
                admission_names = [n for n in missing
                                   if study["opportunities"][n] > admission_start + lead]
            expected_count = screen_count + (4 * len(admission_names) if admission_operations == 1 else 0)
            negative_screen_only = bool(
                screening and screen_count == 3 and admission_operations == 0
                and search["accepted_for_trial"] is False
                and search["end_time_s"] == end
                and row["status"] in ("not_started", "failed", "incomplete", "interrupted")
                and before == after
            )
            spans_valid = bool(operations) and all(
                op["operation"] == "return_forecasts"
                and all(type(t) in (int, float) and math.isfinite(t)
                        for t in (op["start_time_s"], op["end_time_s"], op["cutoff_time_s"]))
                and start <= op["start_time_s"] <= op["end_time_s"] <= end
                and op["cutoff_time_s"] <= cutoff
                and (op["end_time_s"] <= op["cutoff_time_s"] + 1e-7
                     or screen_operations and i == 0 and search["accepted_for_trial"] is False)
                for i, op in enumerate(operations)
            )
            if operations:
                spans_valid = spans_valid and (
                    operations[0]["start_time_s"] == start
                    and operations[-1]["end_time_s"] == end
                    and all(a["end_time_s"] == b["start_time_s"]
                            for a, b in zip(operations, operations[1:]))
                )
            if screen_operations and operations:
                spans_valid = spans_valid and (
                    operations[0]["end_time_s"] == search["end_time_s"]
                    and operations[0]["cutoff_time_s"] == search["cutoff_time_s"]
                )
            if (
                not spans_valid
                or admission_operations not in (0, 1)
                or (admission_operations == 0 and not negative_screen_only)
                or (admission_operations == 1 and not admission_names)
                or (not negative_screen_only and (
                    end > cutoff + 1e-7 or end - start > available + 1e-7))
                or available < envelope["comparison_preparation"]["minimum_start_budget_s"]
                or count_after - count_before != expected_count
            ):
                reasons.append("comparison_calculation_deadline_or_accounting")
        elif end != start or count_after != count_before or operation_start != operation_end:
            reasons.append("comparison_unrecorded_work")
        observed_selections = [
            item for item in study["supervision_observations"] if item["time_s"] <= start
        ]
        if (
            not observed_selections
            or start - observed_selections[-1]["time_s"] > dt
            or observed_selections[-1]["selected"]
            != (None if booking is None else booking["candidate_id"])
        ):
            reasons.append("comparison_booking_observation_binding")
        if booking is not None:
            prior_revisions = [r for r in study["plan_revisions"] if r["time_s"] <= start]
            if (
                not prior_revisions
                or prior_revisions[-1]["candidate_id"] != booking["candidate_id"]
                or prior_revisions[-1]["candidate_sha256"] != booking["candidate_sha256"]
            ):
                reasons.append("comparison_booking_approval_missing")
        later = row["later_observation"]
        if later["state"]["time_s"] != end:
            reasons.append("comparison_preparation_later_observation")
        remaining = [
            name
            for name in requested
            if name not in after or fresh_match(versions[after[name]], later, envelope)
        ]
        if row["remaining_candidate_ids"] != remaining:
            reasons.append("comparison_preparation_readiness")
        if protected is not None and row["selected_after"] != booking:
            reasons.append("comparison_preparation_changed_booking")
        if any(after.get(name) != before[name] for name in reused):
            reasons.append("comparison_valid_forecast_not_reused")
    if previous_count != study["forecast_count"]:
        reasons.append("comparison_forecast_accounting")
    if any(op["operation"] == "return_forecasts"
           for op in study["pending_operations"][previous_operation_end:]):
        reasons.append("comparison_forecast_accounting")
    for record in study["decisions"]:
        request = record["request"]
        context = request.get("comparison_context") or {}
        row = rows.get(context.get("preparation_id"))
        observation = record["request_observation"]
        if (
            row is None
            or observation["state"]["time_s"] != request["time_s"]
            or checksum(request["notice"]) != checksum(notices(expected_case, request["time_s"]))
        ):
            reasons.append("comparison_request_context")
            continue
        generation = checksum(
            {
                key: value
                for key, value in request["notice"].items()
                if key not in ("sequence", "issued_at_s")
            }
        )
        completed = [item for item in preparations if item["end_time_s"] <= request["time_s"]]
        public_hashes = {
            name: item["candidate_sha256"] for name, item in request["candidates"].items()
        }
        if (
            not completed
            or completed[-1]["preparation_id"] != row["preparation_id"]
            or public_hashes != row["candidate_sha256_after"]
        ):
            reasons.append("comparison_request_available_evidence")
        if (
            type(context["ready"]) is not bool
            or context["generation_sha256"] != generation
            or row["generation_sha256"] != generation
            or row["end_time_s"] > request["time_s"]
            or context["missing_candidate_ids"] != row["remaining_candidate_ids"]
            or context["ready"]
            != (not row["remaining_candidate_ids"] and row["status"] != "interrupted")
        ):
            reasons.append("comparison_request_preparation_binding")
        admissible = {}
        for name, public in request["candidates"].items():
            candidate = versions.get(public["candidate_sha256"])
            if (
                candidate is None
                or candidate["id"] != name
                or public["return_time_s"] != candidate["return_time_s"]
                or public["predicted_contacts"] != [metrics(t) for t in candidate["trials"]]
                or any(
                    t["origin"]["state"]["time_s"] > request["time_s"]
                    or t["origin"]["basis"] != "synthetic_navigation_estimate_v1"
                    for t in candidate["trials"]
                )
            ):
                reasons.append("comparison_request_candidate_binding")
                continue
            expected = expected_comparison_material(
                candidate, envelope, request["notice"], study["scheduled_s"]
            )
            if checksum(public.get("comparison_material")) != checksum(expected):
                reasons.append("comparison_candidate_costs")
            check = admit(
                candidate,
                envelope,
                observation,
                request["notice"],
                study["scheduled_s"],
                expected_deployment_binding=expected_deployment_binding,
            )
            if (
                public["constraint_rejections"] != check["reasons"]
                or public["areas"] != check["areas"]
            ):
                reasons.append("comparison_request_admission")
            if check["accepted"] and "select_" + name in request["allowed_actions"]:
                admissible[name] = expected
        quality.append(
            comparison_quality_record(
                record, admissible, envelope["comparison_preparation"]["quality_objectives"]
            )
        )
    report = {
        "schema": "missionos.m1_comparison_quality.v1",
        "role": "posthoc_not_dispatch_or_case_gate",
        "basis": "decision_time_observations_and_sampled_uncertain_estimates",
        "no_scenario_or_fallback_score_comparison": True,
        "objectives": envelope["comparison_preparation"]["quality_objectives"],
        "decisions": quality,
        "counts": {
            key: sum(row["status"] == key for row in quality)
            for key in ("dominated", "not_demonstrably_dominated", "indeterminate")
        },
    }
    return sorted(set(reasons)), report


def verify(
    study,
    *,
    expected_envelope,
    expected_case,
    expected_sources,
    expected_run_id=None,
    expected_deployment_binding=None,
):
    binding = result_binding(
        study,
        expected_envelope=expected_envelope,
        expected_case=expected_case,
        expected_sources=expected_sources,
        expected_run_id=expected_run_id,
    )
    reasons = [
        name + "_unavailable"
        for name in binding["binding_unavailable"]
        if name != "observed_run_id"
    ]
    if expected_run_id is not None and binding["observed_run_id"] != expected_run_id:
        reasons.append("run_identity_binding")
    try:
        if (
            study["envelope"] != expected_envelope
            or study["case"] != expected_case
            or study["source_sha256"] != expected_sources
        ):
            reasons.append("approved_input_binding")
        envelope = expected_envelope
        released_expected = (
            26
            if expected_deployment_binding is None
            else expected_deployment_binding["released_count"]
        )
        if (
            expected_deployment_binding is None
            and "deployment" in study
            or expected_deployment_binding is not None
            and checksum(study.get("deployment_binding")) != checksum(expected_deployment_binding)
        ):
            reasons.append("deployment_authority_binding")
        if (
            study["launch"]["outcome"]["payload_released_count"] != released_expected
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
                expected_deployment_binding=expected_deployment_binding,
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
            if binding["observed_run_id"] is not None and (
                type(request["request_id"]) is not str
                or not request["request_id"].startswith(binding["observed_run_id"] + "-")
            ):
                reasons.append("decision_run_identity")
            reasons.extend(
                planning_window_reasons(
                    record,
                    envelope,
                    study["profile"]["integration"]["coast_dt_s"]
                    if "orbital_planning_window" in envelope
                    else None,
                )
            )
            if record["accepted_action"] is not None:
                if (
                    response is None
                    or record["accepted_action"] not in request["allowed_actions"]
                    or record["accepted_action"] != response.get("action")
                    or record["later_time_s"] > request["deadline_s"]
                ):
                    reasons.append("unapproved_decision")
            if envelope.get("event_supervision"):
                stale = record.get("interrupted_by_events", False)
                expected_fallback = record["accepted_action"] is None and not stale
                if type(record["fallback"]) is not bool or record["fallback"] != expected_fallback:
                    reasons.append("fallback_classification")
            if record["later_observation"]["state"]["time_s"] != record["later_time_s"]:
                reasons.append("decision_observation_time")
            if response is not None and (
                response.get("request_sha256") != checksum(request)
                or response.get("request_id") != request["request_id"]
            ):
                reasons.append("response_binding")
            immediate = record["request_disposition"] == "deadline_fallback"
            if record["later_time_s"] < request["time_s"] or (
                not immediate and record["later_time_s"] == request["time_s"]
            ):
                reasons.append("later_observation")
            reasons.extend(
                deadline_fallback_reasons(
                    record, study, envelope, expected_deployment_binding=expected_deployment_binding
                )
            )
        records = {r["request"]["request_id"]: r for r in study["decisions"]}
        for revision in study["plan_revisions"]:
            decision = records.get(revision["decision_request_id"])
            if decision is None:
                reasons.append("revision_decision_missing")
                continue
            chosen = "select_" + revision["candidate_id"]
            if revision["basis"] == "checked_fallback":
                if decision["accepted_action"] is not None or (
                    envelope.get("event_supervision")
                    and (not decision["fallback"] or decision.get("interrupted_by_events", False))
                ):
                    reasons.append("fallback_falsely_attributed")
            elif decision["accepted_action"] != chosen:
                reasons.append("revision_action_binding")
            elif revision["basis"] == "ai" and not (decision["response"] or {}).get(
                "model_inference_invoked"
            ):
                reasons.append("ai_inference_missing")
        reasons.extend(decision_budget_reasons(
            study["decisions"], envelope["maximum_jev_calls"], envelope=envelope))
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
                expected_deployment_binding=expected_deployment_binding,
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

            reasons.extend(
                verify_events(
                    study,
                    envelope,
                    expected_case=expected_case,
                    expected_deployment_binding=expected_deployment_binding,
                )
            )
            reasons.extend(event_operation_notice_reasons(study, expected_case))
        comparison_report = None
        if envelope.get("comparison_preparation"):
            comparison_reasons, comparison_report = verify_comparison(
                study,
                envelope,
                expected_case,
                expected_deployment_binding=expected_deployment_binding,
            )
            reasons.extend(comparison_reasons)
        completion_reasons = case_reasons(
            study, expected_case, outcome, expected_deployment_binding=expected_deployment_binding
        )
        return {
            **binding,
            "passed": not reasons,
            "issues": sorted(set(reasons)),
            "outcome": outcome,
            "case_accepted": not reasons and not completion_reasons,
            "case_issues": completion_reasons,
            **({"comparison_quality": comparison_report} if comparison_report is not None else {}),
            "mission_completed": False,
            "physical_execution": False,
            "independent_physics_validation": False,
        }
    except (
        KeyError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        IndexError,
        AttributeError,
        OverflowError,
    ) as error:
        return {
            **binding,
            "passed": False,
            "issues": sorted(set(reasons + ["invalid_record:" + type(error).__name__])),
            "outcome": "unresolved",
            "case_accepted": False,
            "case_issues": ["invalid_record"],
            "mission_completed": False,
            "physical_execution": False,
            "independent_physics_validation": False,
        }

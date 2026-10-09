"""Scalar M1 constraint/result checks, independent of the GNC and propagator.

These checks establish recorded constraints in a declared synthetic model, not
physical-model accuracy. No successful forecast flag or controller status is
accepted as a contact measurement.
"""

from hashlib import sha256
import json
import math
from pathlib import Path


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


def event_tradeoff_witnesses(study, executed):
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
                or public["predicted_contacts"] != [metrics(t) for t in candidate["trials"]]
                or candidate["trials"][0]["origin"]["state"]["time_s"] > request["time_s"]
            ):
                continue
            check = admit(
                candidate,
                envelope,
                record["later_observation"],
                request["notice"],
                study["scheduled_s"],
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
        witnesses = event_tradeoff_witnesses(study, executed)
        if not witnesses:
            reasons.append("actual_ai_replanning_not_executed")
        if not any(w["both_options_admissible"] for w in witnesses):
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
        checked_fallback = bool(executed and executed["basis"] == "checked_fallback")
        if expected_case == "event_timeout" and executed and not checked_fallback:
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
    return reasons


def decision_budget_reasons(records, maximum):
    reasons, used = [], 0
    for r in records:
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


def deadline_fallback_reasons(record, study, envelope):
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


def verify(study, *, expected_envelope, expected_case, expected_sources, expected_run_id=None):
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
            if binding["observed_run_id"] is not None and (
                type(request["request_id"]) is not str
                or not request["request_id"].startswith(binding["observed_run_id"] + "-")
            ):
                reasons.append("decision_run_identity")
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
            reasons.extend(deadline_fallback_reasons(record, study, envelope))
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

            reasons.extend(verify_events(study, envelope, expected_case=expected_case))
            reasons.extend(event_operation_notice_reasons(study, expected_case))
        completion_reasons = case_reasons(study, expected_case, outcome)
        return {
            **binding,
            "passed": not reasons,
            "issues": sorted(set(reasons)),
            "outcome": outcome,
            "case_accepted": not reasons and not completion_reasons,
            "case_issues": completion_reasons,
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

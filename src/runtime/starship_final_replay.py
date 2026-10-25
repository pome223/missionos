"""Pinned saved-only display mapping of one final-v2 or final-v3 mission.

This is neither a verifier nor a completed-v1 compatibility wrapper. All paths
are JSON pointers into the pinned current mission/host blobs. A failed physical
or consistency result yields a no-video summary, never a success projection.
The renderer consumes recorded quaternions and finite actuators; internal model
reasoning, predicted trajectories and later satellite propagation are absent.
"""

from copy import deepcopy
import math
import re

from .starship_restored_replay import _read, _digest, _same, _state, _track, _events

SCHEMA = "missionos.starship_final_replay.v1"
MISSION = "missionos.starship_final_native_mission.v2"
VERIFICATION = "missionos.starship_final_native_verification.v2"
SUPPORTED_SCHEMA_PAIRS = frozenset({
    (MISSION, VERIFICATION),
    ("missionos.starship_final_native_mission.v3",
     "missionos.starship_final_native_verification.v3"),
})
HOST = "missionos.starship_final_host_evidence.v1"
HOSTS = frozenset({"restored", "supplemental", "visual", "sensor"})
NATIVE_ROLES = frozenset(
    {
        "ascent_supervision",
        "booster_continuation_review",
        "deployment",
        "return_selection",
        "visual_observation",
        "return_sensor_judgment",
        "deep_comparison",
    }
)
MAXIMUM_INPUT_BYTES = 512 * 1024**2
FIDELITY_LIMITS = (
    "Development surrogate, not SpaceX flight software or a real-vehicle validation.",
    "This mission does not reverify current-source Flight 14 altitude/velocity agreement; historical fits must not be relabelled as current measurements.",
    "Booster acceptance uses the separately registered 10 km development water-entry envelope, not the older 5 km footprint, water/structure survival or tower catch.",
    "Trouble selection covers a finite synthetic normal/propulsion/sensor pool; probabilities are test settings, not incident rates or arbitrary-fault qualification.",
    "The old 39-case return qualification is unchanged; exact saved consistency is neither refreshed legacy certification nor operational authorization.",
)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def source(pointer, record="mission"):
    return {"record": record, "pointer": pointer}


def brief(value):
    require(type(value) is str and len(value) <= 600, "final_replay_external_summary_budget")
    return value


def _references(judgment):
    rows = judgment.get("references", [])
    require(type(rows) is list and len(rows) <= 16, "final_replay_reference_budget")
    values = []
    for item in rows:
        require(
            type(item) is dict
            and set(item) == {"reference_id", "value"}
            and type(item["reference_id"]) is str
            and 0 < len(item["reference_id"]) <= 256
            and (type(item["value"]) is bool or number(item["value"])),
            "final_replay_finite_reference_required",
        )
        values.append(deepcopy(item))
    return values


def _classification(judgment, mode):
    receipt = judgment.get("provider_receipt", {})
    require(type(receipt) is dict, "final_replay_provider_receipt_required")
    invoked = judgment.get("model_inference_invoked")
    native = (
        mode == "live"
        and invoked is True
        and receipt.get("model_inference_invoked") is True
        and receipt.get("mode") == "live"
        and receipt.get("status") == "succeeded"
        and receipt.get("complete_response_observed") is True
        and receipt.get("call_succeeded") is True
        and judgment.get("status") == "valid"
    )
    fixture = (
        mode == "fixture"
        and invoked is False
        and receipt.get("model_inference_invoked") is False
        and receipt.get("mode") in ("fixture", "synthetic_fixture")
        and judgment.get("status") in ("valid", "fixture_valid")
    )
    require(
        not (
            mode == "fixture"
            and (invoked is True or receipt.get("model_inference_invoked") is True)
        ),
        "final_replay_fixture_cannot_claim_native_inference",
    )
    return (
        "confirmed_native_judgment"
        if native
        else "fixed_response"
        if fixture
        else "unavailable_or_unaccepted"
    )


def _judgment(request, result, *, mode, phase, origin, provider=None, applied_time=None):
    require(type(request) is dict and type(result) is dict, "final_replay_saved_judgment_required")
    issued = request.get("time_s", request.get("issued_at_s", request.get("snapshot_time_s")))
    deadline = request.get("deadline_s")
    require(
        number(issued) and number(deadline) and issued < deadline,
        "final_replay_judgment_original_simulation_epoch",
    )
    receipt = result.get("provider_receipt", {})
    row = {
        "request_id": request["request_id"],
        "request_time_s": issued,
        "request_deadline_s": deadline,
        "phase": phase,
        "provider": provider or result.get("provider") or receipt.get("provider"),
        "provider_model": receipt.get("model_id") or receipt.get("request_model"),
        "classification": _classification(result, mode),
        "status": result.get("status"),
        "action": result.get("action"),
        "external_summary": brief(result.get("summary", "")),
        "references": _references(result),
        "native_choice_probabilities": deepcopy(receipt.get("native_choice_probabilities")),
        "native_choice_confidence": receipt.get("native_choice_confidence"),
        "free_prose_verified": False,
        "internal_reasoning_recorded": False,
        "proposal_is_approval_or_dispatch": False,
        "resolution_simulation_time_inferred": False,
        "source": origin,
    }
    if applied_time is not None:
        require(
            number(applied_time) and issued <= applied_time <= deadline,
            "final_replay_saved_application_time_required",
        )
        row["observed_disposition_time_s"] = applied_time
    return row


def _hosts(raw, pins, *, plan, manifest, context, mode, maximum):
    require(
        type(raw) is dict and set(raw) == HOSTS and type(pins) is dict and set(pins) == HOSTS,
        "final_replay_four_external_host_pins_required",
    )
    rows = {}
    for channel in sorted(HOSTS):
        value = _read(raw[channel], pins[channel], "host_" + channel, maximum_input_bytes=maximum)
        require(
            type(value) is dict
            and set(value)
            == {"schema", "channel", "plan_sha256", "source_sha256", "context", "mode", "record"}
            and value["schema"] == HOST
            and value["channel"] == channel
            and value["plan_sha256"] == plan
            and value["source_sha256"] == manifest
            and _same(value["context"], context)
            and value["mode"] == mode
            and type(value["record"]) is dict,
            "final_replay_host_binding_changed",
        )
        rows[channel] = value["record"]
    return rows


def _coverage(verified, mode):
    work = verified.get("work", {})
    roles = work.get("native_role_work_observed", {key: False for key in NATIVE_ROLES})
    require(
        type(roles) is dict
        and set(roles) == NATIVE_ROLES
        and all(type(x) is bool for x in roles.values()),
        "final_replay_independent_native_role_inventory",
    )
    all_native = mode == "live" and all(roles.values())
    require(
        work.get("all_native_role_work_observed", False) is all_native
        and (mode != "fixture" or not any(roles.values())),
        "final_replay_verified_role_claim_changed",
    )
    return {
        "native_role_work_observed": deepcopy(roles),
        "all_requested_native_roles_observed": all_native,
        "missing_native_roles": sorted(key for key, value in roles.items() if not value),
        "scripted_scope_amendment_consistent": work.get(
            "scripted_scope_amendment_consistent", False
        ),
        "sensor_tracking_requested": work.get("sensor_tracking_requested", False),
        "sensor_once_only_application": work.get("sensor_once_only_application", False),
        "sensor_fallback_count": work.get("sensor_fallback_count", 0),
        "native_role_observed_is_judgment_quality_score": False,
        "real_human_review": False,
        "missing_roles_filled_from_other_flights": False,
        "tower_catch_in_scope": False,
        "source": source("/work", "verification"),
    }


def _fidelity(notes, manifest):
    if notes is None:
        notes = {
            "source_manifest_sha256": manifest,
            "real_vehicle_validated": False,
            "current_public_data_fit_reverified": False,
            "notes": [],
            "references": [],
        }
    require(
        type(notes) is dict
        and set(notes)
        == {
            "source_manifest_sha256",
            "real_vehicle_validated",
            "current_public_data_fit_reverified",
            "notes",
            "references",
        }
        and notes["source_manifest_sha256"] == manifest
        and notes["real_vehicle_validated"] is False
        and notes["current_public_data_fit_reverified"] is False
        and type(notes["notes"]) is list
        and len(notes["notes"]) <= 20
        and all(type(x) is str and 0 < len(x) <= 600 for x in notes["notes"])
        and type(notes["references"]) is list
        and len(notes["references"]) <= 20,
        "final_replay_fidelity_disclosure_required",
    )
    for reference in notes["references"]:
        require(
            type(reference) is dict
            and set(reference) == {"path", "sha256"}
            and type(reference["path"]) is str
            and re.fullmatch(r"[A-Za-z0-9_.\-/]+", reference["path"])
            and not reference["path"].startswith("/")
            and ".." not in reference["path"].split("/")
            and identity(reference["sha256"]),
            "final_replay_fidelity_relative_reference",
        )
    return {
        "schema": "missionos.starship_final_replay_fidelity.v1",
        **deepcopy(notes),
        "mandatory_limits": list(FIDELITY_LIMITS),
        "external_notes_reverified_by_display": False,
        "historical_measurements_are_current_measurements": False,
    }


def _coast_samples(handoff):
    rows = handoff["coast_history"]
    require(type(rows) is list and rows, "final_replay_saved_coast_history_required")
    return [{**_state(row["state"]), "phase": "orbital_coast", "body_id": "ship"} for row in rows]


def _body_tracks(mission):
    prefix, handoff, sensor = (mission[key] for key in ("prefix", "handoff", "sensor_return"))
    rollout, booster = sensor["rollout"], prefix["booster_run"]
    coast = _coast_samples(handoff)
    require(
        prefix["restored_prefix"]["closed"] is True
        and _same(_state(prefix["samples"][-1]), prefix["final_state"])
        and _same(prefix["final_state"], _state(coast[0]))
        and _same(_state(coast[-1]), handoff["snapshot"]["state"])
        and _same(handoff["snapshot"]["state"], sensor["initial_state"])
        and _same(sensor["initial_state"], rollout["initial_state"])
        and _same(_state(rollout["samples"][0]), sensor["initial_state"])
        and _same(_state(rollout["samples"][-1]), rollout["final_state"])
        and _same(sensor["handoff_record"], handoff)
        and sensor["handoff_sha256"] == _digest(handoff),
        "final_replay_exact_actual_state_seams_required",
    )
    inventory = handoff["inventory"]
    released, retained = inventory["released_count"], inventory["retained_count"]
    require(
        type(released) is type(retained) is int
        and released + retained == 26
        and retained in (0, 13, 26)
        and len(prefix["satellites"]) == released
        and _same([row["receipt"] for row in prefix["satellites"]], inventory["release_receipts"])
        and _same(sensor["inventory"], inventory),
        "final_replay_inventory_receipts_changed",
    )
    events = _events(
        prefix["events"],
        "/prefix/events",
        prefix["samples"][0]["time_s"],
        prefix["final_state"]["time_s"],
    )
    events += _events(
        rollout["events"],
        "/sensor_return/rollout/events",
        rollout["initial_state"]["time_s"],
        rollout["final_state"]["time_s"],
    )
    booster_events = _events(
        booster["events"],
        "/prefix/booster_run/events",
        booster["samples"][0]["time_s"],
        booster["final_state"]["time_s"],
    )
    ship = _track(
        "ship",
        [
            ("/prefix/samples", prefix["samples"]),
            ("/handoff/coast_history", coast),
            ("/sensor_return/rollout/samples", rollout["samples"]),
        ],
        events,
    )
    require(
        ship["start_time_s"] == 0.0
        and _same(booster["booster_separation_state"], prefix["booster_separation_state"])
        and _same(_state(booster["samples"][0]), prefix["booster_separation_state"])
        and _same(_state(booster["samples"][-1]), booster["final_state"]),
        "final_replay_ground_and_actual_booster_origin_required",
    )
    for segment in ship["segments"]:
        for point in segment["samples"]:
            if point["source"]["pointer"].startswith("/handoff/coast_history/"):
                point["source"]["pointer"] += "/state"
                point["phase_source"] = source("/handoff/snapshot/phase")
                point["unavailable_display_fields"] = [
                    "applied_thrust_n",
                    "main_engine_thrust_n",
                    "mass_kg",
                    "com_z_m",
                ]
    bodies, releases, identities = (
        [
            ship,
            _track(
                "booster", [("/prefix/booster_run/samples", booster["samples"])], booster_events
            ),
        ],
        [],
        set(),
    )
    for index, child in enumerate(prefix["satellites"]):
        name, receipt = child["id"], child["receipt"]
        require(
            type(name) is str
            and 0 < len(name) <= 80
            and name not in identities | {"ship", "booster"}
            and child.get("orbital_propagation_only") is True
            and _same(_state(child["samples"][0]), child["release_state"])
            and child["samples"][0]["time_s"] == receipt["time_s"],
            "final_replay_child_release_origin_required",
        )
        identities.add(name)
        track = _track(name, [(f"/prefix/satellites/{index}/samples", child["samples"])], [])
        require(
            track["end_time_s"] <= prefix["final_state"]["time_s"],
            "final_replay_new_satellite_propagation_prohibited",
        )
        track["propagation_scope"] = "saved_post_release_orbit_only_no_operational_service"
        bodies.append(track)
        releases.append(
            {
                "satellite_id": name,
                "time_s": receipt["time_s"],
                "released_count_after": index + 1,
                "retained_count_after": 25 - index,
                "source": source(f"/prefix/satellites/{index}/receipt"),
            }
        )
    return bodies, releases, events + booster_events


def _judgments(mission, hosts):
    mode, judgments = mission["execution"]["mode"], []
    for index, session in enumerate(hosts["restored"]["decision_host"]["requests"]):
        request = session["frame"]["request"]
        require(
            request["phase"] in ("deployment", "return"),
            "final_replay_legacy_host_is_pre_dispatch_only",
        )
        for attempt, value in enumerate(session["attempts"]):
            row = _judgment(
                request,
                value.get("judgment") or {},
                mode=mode,
                phase="pre_dispatch_" + request["phase"],
                provider=value["provider"],
                origin=source(
                    f"/record/decision_host/requests/{index}/attempts/{attempt}", "host.restored"
                ),
            )
            review = (session.get("delivered_result") or {}).get("return_review")
            if type(review) is dict:
                row["return_review_route"] = {
                    "route": review.get("route"),
                    "review_required": review.get("review_required"),
                    "rules_review_is_jev_delegation": review.get("route")
                    == "explicit_jev_delegation",
                    "source": source(
                        f"/record/decision_host/requests/{index}/delivered_result/return_review",
                        "host.restored",
                    ),
                }
            judgments.append(row)
    supervision = mission["supplemental"]["supervision"]
    requests = {row["request_id"]: row["request"] for row in supervision["requests"]}
    for index, item in enumerate(supervision["judgments"]):
        result = item.get("result") or {}
        row = _judgment(
            requests[item["request_id"]],
            result,
            mode=mode,
            phase=item["domain"],
            applied_time=item["time_s"] if item["accepted"] else None,
            origin=source(f"/supplemental/supervision/judgments/{index}"),
        )
        row["adopted_by_worker"] = item["accepted"]
        row["safety_path"] = item.get("safety_path")
        if not item["accepted"]:
            row["observed_unavailable_time_s"] = item["time_s"]
        judgments.append(row)
    visual = mission["supplemental"]["visual"]
    requests = {item["envelope_sha256"]: item["request"] for item in visual["requests"]}
    for index, item in enumerate(visual["results"]):
        reply = item.get("reply")
        result = reply.get("result") or {} if type(reply) is dict else {}
        row = _judgment(
            requests[item["envelope_sha256"]],
            result,
            mode=mode,
            phase="visual_observation",
            provider="typesafe",
            applied_time=item["time_s"] if item["accepted"] else None,
            origin=source(f"/supplemental/visual/results/{index}"),
        )
        row["adopted_by_worker"] = item["accepted"]
        row["observation_choice"] = result.get("observation_choice")
        diagnosis = result.get("visual_diagnosis")
        if type(diagnosis) is dict:
            row["visual_findings"] = {
                key: diagnosis[key]
                for key in (
                    "visibility",
                    "deployment_visual_state",
                    "telemetry_relation",
                    "recommended_action",
                    "observation_choice",
                )
                if key in diagnosis
            }
        if not item["accepted"]:
            row["safety_action"] = "withhold_deployment"
            row["observed_unavailable_time_s"] = item["time_s"]
        judgments.append(row)
    for index, session in enumerate(hosts["sensor"]["sessions"]):
        for number, call in enumerate(session["assessments"]):
            judgments.append(
                _judgment(
                    session["request"],
                    call,
                    mode=mode,
                    phase="return_sensor_judgment",
                    origin=source(f"/record/sessions/{index}/assessments/{number}", "host.sensor"),
                )
            )
    return sorted(judgments, key=lambda row: row["request_time_s"])


def _timeline(mission, hosts, initial):
    rows, governance = list(initial), mission["governance"]
    for index, item in enumerate(mission["supplemental"]["supervision"]["judgments"]):
        rows.append(
            {
                "time_s": item["time_s"],
                "event": item["domain"] + "_proposal_disposition",
                "kind": "recorded_proposal_not_dispatch"
                if item["accepted"]
                else "preapproved_safety_path_not_ai_judgment",
                "action": item["action"],
                "accepted": item["accepted"],
                "safety_path": item.get("safety_path"),
                "source": source(f"/supplemental/supervision/judgments/{index}"),
            }
        )
    delivery = mission["sensor_return"].get("tracking_delivery")
    if delivery is not None:
        rows.extend(
            {
                "time_s": delivery[key],
                "event": event,
                "kind": "recorded_independent_observation_tool_fact",
                "source": source("/sensor_return/tracking_delivery"),
            }
            for key, event in (
                ("requested_at_s", "independent_tracking_requested"),
                ("received_at_s", "independent_tracking_delivered"),
            )
        )
    for index, item in enumerate(governance["deployment"]["decisions"]):
        rows.append(
            {
                "time_s": item["applied_at_actual_time_s"],
                "event": "deployment_judgment_applied",
                "kind": "preapproved_fallback"
                if item["fallback_used"]
                else "recorded_judgment_application",
                "action": item["applied_action"],
                "fallback_used": item["fallback_used"],
                "source": source(f"/governance/deployment/decisions/{index}"),
            }
        )
    for index, item in enumerate(governance["decisions"]):
        rows.append(
            {
                "time_s": item["actual_snapshot"]["state"]["time_s"],
                "event": "return_proposal_disposition",
                "kind": "recorded_judgment_disposition",
                "action": item["original_action"],
                "disposition": item["disposition"],
                "source": source(f"/governance/decisions/{index}"),
            }
        )
    for index, item in enumerate(governance["current_origin_changes"]):
        rows.append(
            {
                "time_s": item["actual_snapshot"]["state"]["time_s"],
                "event": "reservation_changed",
                "kind": "recorded_reservation_not_dispatch",
                "source": source(f"/governance/current_origin_changes/{index}"),
            }
        )
        if item.get("confirmation_snapshot") is not None:
            rows.append(
                {
                    "time_s": item["confirmation_snapshot"]["state"]["time_s"],
                    "event": "later_reservation_observed",
                    "kind": "recorded_confirmation_not_contact",
                    "source": source(
                        f"/governance/current_origin_changes/{index}/confirmation_snapshot"
                    ),
                }
            )
    for index, item in enumerate(hosts["restored"].get("operator_ledger", [])):
        for key, event in (
            ("request", "scope_amendment_requested"),
            ("receipt", "scripted_scope_choice_received"),
            ("consumption", "scope_grant_consumed_once"),
        ):
            value = item.get(key)
            if value is not None:
                rows.append(
                    {
                        "time_s": value[
                            "issued_simulation_time_s" if key == "request" else "simulation_time_s"
                        ],
                        "event": event,
                        "kind": "authenticated_scripted_test_principal_not_real_human_review",
                        "approved": value.get("approved"),
                        "choice": value.get("choice"),
                        "source": source(f"/record/operator_ledger/{index}/{key}", "host.restored"),
                    }
                )
    rows.append(
        {
            "time_s": mission["handoff"]["snapshot"]["state"]["time_s"],
            "event": "actual_owner_handoff",
            "kind": "rules_dispatch_then_actual_state_handoff_not_contact",
            "source": source("/handoff"),
        }
    )
    injection = mission["fault_harness"]["injection"]
    if injection is not None:
        rows.append(
            {
                "time_s": injection["injection_time_s"],
                "event": "registered_test_harness_selection_consumed",
                "kind": "harness_fact_not_model_input",
                "family": mission["fault_harness"]["selection"]["selected_family"],
                "source": source("/fault_harness/injection"),
            }
        )
    for index, item in enumerate(mission["supplemental"]["visual"]["events"]):
        if number(item.get("time_s")):
            rows.append(
                {
                    "time_s": item["time_s"],
                    "event": item["event"],
                    "kind": "recorded_visual_observation_gate",
                    "source": source(f"/supplemental/visual/events/{index}"),
                }
            )
    for index, item in enumerate(mission["sensor_return"]["fallback_ledger"]):
        rows.append(
            {
                "time_s": item["time_s"],
                "event": "preapproved_sensor_fallback",
                "kind": "preapproved_fallback_not_ai_judgment",
                "action": item["action"],
                "source": source(f"/sensor_return/fallback_ledger/{index}"),
            }
        )
    require(
        all(number(row["time_s"]) and 0 <= row["time_s"] <= 16000.0 for row in rows),
        "final_replay_finite_event_clock",
    )
    return sorted(rows, key=lambda row: row["time_s"])


def build_final_replay(
    mission_raw,
    verification_raw,
    *,
    expected_mission_sha256,
    expected_verification_sha256,
    expected_plan_sha256,
    expected_source_sha256,
    host_records,
    expected_host_sha256,
    public_data_fidelity=None,
    maximum_input_bytes=MAXIMUM_INPUT_BYTES,
):
    """Map externally pinned final evidence, without files, inference or dynamics.

    A pinned verifier is the caller's reviewed saved-check result; hashes are
    not signatures and this function does not reexecute it. Negative evidence
    remains a no-video summary. Even accepted physical replay has independent
    native-role coverage and may be an incomplete all-features showcase.
    """
    require(
        type(maximum_input_bytes) is int
        and 0 < maximum_input_bytes <= MAXIMUM_INPUT_BYTES
        and identity(expected_plan_sha256)
        and type(expected_source_sha256) is dict
        and expected_source_sha256
        and all(
            type(key) is str and identity(value) for key, value in expected_source_sha256.items()
        ),
        "final_replay_external_source_plan_and_budget_required",
    )
    require(
        type(host_records) is dict
        and all(type(raw) is bytes for raw in host_records.values())
        and type(mission_raw) is type(verification_raw) is bytes
        and sum(map(len, [mission_raw, verification_raw, *host_records.values()]))
        <= maximum_input_bytes,
        "final_replay_total_input_budget",
    )
    mission = _read(
        mission_raw, expected_mission_sha256, "mission", maximum_input_bytes=maximum_input_bytes
    )
    verified = _read(
        verification_raw,
        expected_verification_sha256,
        "verification",
        maximum_input_bytes=maximum_input_bytes,
    )
    require(
        (mission.get("schema"), verified.get("schema")) in SUPPORTED_SCHEMA_PAIRS
        and verified.get("record_sha256") == expected_mission_sha256,
        "final_replay_pinned_matching_final_schema_required",
    )
    execution = mission["execution"]
    manifest = _digest(expected_source_sha256)
    require(
        execution.get("schema") == "missionos.starship_final_execution.v2"
        and execution.get("plan_sha256") == verified.get("plan_sha256") == expected_plan_sha256
        and _same(execution.get("source_sha256"), expected_source_sha256)
        and verified.get("source_sha256") == manifest
        and execution.get("mode") in ("fixture", "live")
        and execution["mode"] == verified.get("mode")
        and _same(execution.get("context"), verified.get("context"))
        and execution.get("case_id") == verified.get("case_id"),
        "final_replay_plan_source_context_or_mode_changed",
    )
    mode = execution["mode"]
    hosts = _hosts(
        host_records,
        expected_host_sha256,
        plan=expected_plan_sha256,
        manifest=manifest,
        context=execution["context"],
        mode=mode,
        maximum=maximum_input_bytes,
    )
    coverage = _coverage(verified, mode)
    output = {
        "schema": SCHEMA,
        "case_id": execution["case_id"],
        "source_run_id": execution["context"]["run_id"],
        "mode": mode,
        "playback_available": False,
        "status": "retained_negative_no_video",
        "bodies": [],
        "release_receipts": [],
        "judgments": [],
        "timeline": [],
        "role_coverage": coverage,
        "verified_outcome": deepcopy(verified.get("outcome")),
        "verification_issues": deepcopy(verified.get("issues", [])),
        "public_data_fidelity": _fidelity(public_data_fidelity, manifest),
        "provenance": {
            "mission_raw_sha256": expected_mission_sha256,
            "verification_raw_sha256": expected_verification_sha256,
            "host_raw_sha256": deepcopy(expected_host_sha256),
            "plan_sha256": expected_plan_sha256,
            "source_manifest_sha256": manifest,
            "external_hash_pairing_is_not_signature_authentication": True,
        },
        "display_policy": {
            "autoplay": False,
            "loop": False,
            "recorded_points_only": True,
            "predictions_included": False,
            "new_satellite_propagation": False,
            "new_physical_states": False,
            "metrics_recomputed_from_display": False,
            "physical_verifier_reexecuted": False,
            "legacy_completed_v1_flight_fabricated": False,
            "internal_reasoning_invented": False,
            "render_or_video_produced": False,
            "physical_execution": False,
        },
    }
    if (
        verified.get("passed") is not True
        or verified.get("issues", [])
        or any(
            verified.get("outcome", {}).get(key) is not True
            for key in (
                "all_required_physical_gates_met",
                "ship_contact_limits_met",
                "booster_contact_envelope_met",
            )
        )
    ):
        return output
    require(
        mission["governance"].get("schema") == "missionos.starship_final_pre_dispatch_governance.v1"
        and mission["governance"].get("completed_v1_flight") is False
        and mission["handoff"].get("completed_flight") is False,
        "final_replay_cannot_fabricate_legacy_completed_flight",
    )
    bodies, releases, events = _body_tracks(mission)
    require(
        type(verified["outcome"].get("contact")) is dict
        and verified["outcome"]["contact"].get("time_s") == bodies[0]["end_time_s"],
        "final_replay_verified_contact_endpoint_changed",
    )
    judgments = _judgments(mission, hosts)
    output["judgment_counts"] = {
        kind: sum(row["classification"] == kind for row in judgments)
        for kind in ("confirmed_native_judgment", "fixed_response", "unavailable_or_unaccepted")
    }
    output.update(
        playback_available=True,
        status="verified_physical_simulation_replay",
        bodies=bodies,
        release_receipts=releases,
        judgments=judgments,
        timeline=_timeline(mission, hosts, events),
        clock={
            "type": "absolute_mission_simulation",
            "unit": "s",
            "origin_time_s": 0.0,
            "start_time_s": bodies[0]["start_time_s"],
            "end_time_s": bodies[0]["end_time_s"],
            "wall_clock_not_retimed_as_simulation": True,
        },
        coordinates={
            "position": "ECI",
            "position_unit": "m",
            "velocity_unit": "m/s",
            "angular_rate_unit": "rad/s",
            "quaternion": "scalar-first Hamilton body-to-ECI",
            "body_axis": "+Z",
        },
        renderer_contract={
            "factory": "createMissionOSStarshipCG",
            "attitude_source": "integrated_quaternion",
            "matrix_projection": "recorded body-to-ECI quaternion projected into current local east/up/south; display-only",
            "attitude_inferred_from_thrust_or_velocity": False,
            "actuators": "last saved actual engine_states/flap_angles_rad",
            "interpolation": "optional display-only within strictly increasing segment; never across boundaries",
            "unknown_thrust_or_geometry": "unavailable; never infer lit engines or physical margins",
            "ended_tracks": "held saved endpoint in its last local frame; no continued physical motion",
            "satellites_after_prefix": "record ended; no propagation to Ship contact time",
            "tower": "decorative launch tower only; no catch_geometry or catch success",
            "profile_geometry_source": source("/sensor_return/profile/geometry"),
            "launch_source": source("/sensor_return/profile/launch"),
        },
    )
    return output

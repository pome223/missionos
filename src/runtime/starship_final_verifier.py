"""Saved final-v3 consistency checks; never a flight, provider or permission.

The expected projection and raw host pins come from the externally frozen
plan. Pre-dispatch v1 checks remain intact and stop at the actual owner handoff;
they are never fed a fabricated completed v1 flight or a changed-vehicle return.
All later physical/AI findings require their own saved evidence checks.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sys

from . import starship_final_shared as shared

SCHEMA = "missionos.starship_final_native_mission.v3"
INPUT_SCHEMA = "missionos.starship_final_verification_inputs.v1"
HOST_SCHEMA = "missionos.starship_final_host_evidence.v1"
VERIFICATION_SCHEMA = "missionos.starship_final_native_verification.v3"
HOST_CHANNELS = frozenset({"restored", "supplemental", "visual", "sensor"})
VERIFICATION_INPUT_KEYS = frozenset({"schema", "context", "plan_sha256", "case_id", "mode", "source_commit",
    "source_sha256", "backend", "input_sha256", "profile", "site_catalog", "goal", "ascent_settings",
    "control_settings", "integration_policy", "operations", "visual_policy", "sensor_policy",
    "supervision_policy", "bounds", "authority", "fault_selection", "return_context_projection", "return_review_policy"})
RECORD_KEYS = frozenset({"schema", "execution", "prefix", "governance", "handoff", "fault_harness",
    "sensor_return", "supplemental", "numerical_integration", "claim_limits"})
CLAIM_LIMITS = {"physical_execution": False, "old_certificate_admitted": False,
    "qualification_refreshed": False, "continuous_uncertainty_envelope_verified": False,
    "independent_physical_model": False, "real_vehicle_validated": False, "tower_catch_verified": False,
    "public_data_alignment_proved_by_this_record": False, "host_signature_verified_by_saved_checker": False,
    "human_identity_authenticated_by_saved_checker": False, "known_public_data_gaps_must_accompany_replay": True}
MAXIMUM_RAW_BYTES = 512*1024**2
MAXIMUM_ARTIFACT_BYTES = 1024*1024**2


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _wire(value):
    return shared.wire(value)


def _same(left, right):
    return _wire(left) == _wire(right)


def _hash(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _raw(raw, expected_hash, *, maximum_bytes=MAXIMUM_RAW_BYTES):
    _require(type(raw) is bytes and type(maximum_bytes) is int
        and 0 < len(raw) <= maximum_bytes <= MAXIMUM_RAW_BYTES and _hash(expected_hash)
        and sha256(raw).hexdigest() == expected_hash, "final_saved_raw_hash_type_or_budget")
    return shared.parse(raw)


def validate_verification_inputs(value):
    """Validate a plan projection; never infer missing fields from a result."""
    from .starship_inflight_runtime import _context
    from .starship_restored_contract import _manifest
    from .starship_final_faults import validate_selection
    from .starship_restored_integration import RestoredIntegrationPolicy
    from .starship_state_mission_metrics import _settings
    from .starship_booster_terminal_metrics import _settings as control_settings
    row = shared.parse(_wire(value))
    _require(type(row) is dict and set(row) == VERIFICATION_INPUT_KEYS and row["schema"] == INPUT_SCHEMA,
        "final_external_verification_projection_required")
    _context(row["context"])
    _manifest(row["source_sha256"])
    _require(_hash(row["plan_sha256"]) and row["mode"] in ("fixture", "live")
        and type(row["source_commit"]) is str and re.fullmatch(r"[a-f0-9]{40}", row["source_commit"])
        and type(row["case_id"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["case_id"])
        and type(row["backend"]) is dict and set(row["backend"]) == {
            "python", "platform", "machine", "numpy", "scipy", "executable_sha256"}
        and all(type(item) is str and item for item in row["backend"].values())
        and _hash(row["backend"]["executable_sha256"]), "final_external_identity_backend_or_mode")
    _require(type(row["input_sha256"]) is dict and row["input_sha256"]
        and all(type(name) is str and _hash(identity) for name, identity in row["input_sha256"].items())
        and row["input_sha256"].get(shared.PROFILE) == row["source_sha256"].get(shared.PROFILE),
        "final_external_input_manifest_required")
    for name in row["input_sha256"]:
        path = Path(name)
        _require(name == path.as_posix() and not path.is_absolute() and path.parts
            and ".." not in path.parts, "final_external_input_path")
    _settings(row["ascent_settings"], row["profile"])
    control_settings(row["control_settings"])
    policy = row["integration_policy"]
    integration = RestoredIntegrationPolicy.from_record(policy)
    _require(integration.subdivisions == 2
        and type(row["return_context_projection"]) is bool, "final_external_integration_or_projection")
    if row["return_review_policy"] is not None:
        from .starship_restored_return_review import validate_policy
        validate_policy(row["return_review_policy"])
    for name in ("site_catalog", "goal", "operations", "visual_policy", "sensor_policy", "supervision_policy",
                 "bounds", "authority"):
        _require(type(row[name]) is dict and row[name], "final_external_configuration_incomplete")
    from .starship_final_visual import FinalVisualPolicy
    from .starship_final_prefix_supervision import FinalPrefixSupervisionPolicy
    from .starship_final_sensor_execution import FinalSensorExecutionPolicy
    from dataclasses import fields
    for name, cls in (("visual_policy", FinalVisualPolicy), ("supervision_policy", FinalPrefixSupervisionPolicy)):
        values = {field.name: row[name][field.name] for field in fields(cls)}
        _require(_same(cls(**values).record(), row[name]), "final_external_policy_shape_or_values")
    _require(_same(asdict(FinalSensorExecutionPolicy(**row["sensor_policy"])), row["sensor_policy"])
        and row["sensor_policy"]["mode"] == row["mode"], "final_external_sensor_policy_or_mode")
    validate_selection(row["fault_selection"])
    return row


def execution_binding(inputs):
    row = validate_verification_inputs(inputs)
    return {"schema": "missionos.starship_final_execution.v2",
        **{key: row[key] for key in ("context", "plan_sha256", "case_id", "mode", "source_commit", "source_sha256", "backend")},
        "profile_sha256": row["input_sha256"][shared.PROFILE],
        "settings_sha256": shared.digest(row["ascent_settings"]),
        "control_settings_sha256": shared.digest(row["control_settings"]),
        "integration_policy_sha256": shared.digest(row["integration_policy"]),
        "fault_selection_sha256": row["fault_selection"]["selection_sha256"],
        "verification_inputs_sha256": shared.digest(row)}


def verify_host_wrappers(host_records, expected_hashes, inputs):
    """Bind separately retained host bytes; content hashes are not signatures."""
    _require(type(host_records) is dict and set(host_records) == HOST_CHANNELS
        and type(expected_hashes) is dict and set(expected_hashes) == HOST_CHANNELS,
        "final_complete_external_host_pins_required")
    result = {}
    for channel in sorted(HOST_CHANNELS):
        row = _raw(host_records[channel], expected_hashes[channel])
        _require(type(row) is dict and set(row) == {"schema", "channel", "plan_sha256", "source_sha256", "context", "mode", "record"}
            and row["schema"] == HOST_SCHEMA and row["channel"] == channel
            and row["plan_sha256"] == inputs["plan_sha256"]
            and row["source_sha256"] == shared.digest(inputs["source_sha256"])
            and _same(row["context"], inputs["context"]) and row["mode"] == inputs["mode"]
            and type(row["record"]) is dict, "final_host_wrapper_channel_or_frozen_binding_changed")
        result[channel] = row["record"]
    return result


def verify_current_binding(inputs):
    """Read current source/backend explicitly, not a producer verification flag."""
    required = {"src/runtime/starship_final_verifier.py", "src/runtime/starship_final_handoff.py",
        "src/runtime/starship_final_faults.py", "src/runtime/starship_final_sensor_prediction.py",
        "src/runtime/starship_restored_native_verifier.py", "src/runtime/starship_final_shared.py",
        "src/runtime/starship_final_native.py", "src/runtime/starship_final_outer.py", "src/runtime/starship_final_tasks.py",
        "src/runtime/starship_final_plan.py", "src/runtime/starship_final_gateway.py", "src/runtime/starship_final_protocol.py",
        "src/runtime/starship_final_prefix_verifier.py",
        "src/runtime/starship_final_prefix_supervision.py", "src/runtime/starship_final_visual.py",
        "src/runtime/starship_final_sensor_execution.py", "src/runtime/starship_final_sensor_guard.py",
        "src/intelligence/starship_final_supervision_agent.py", "src/intelligence/starship_final_sensor_agent.py"}
    _require(required <= inputs["source_sha256"].keys(), "final_verifier_sources_not_registered")
    shared.check_manifest(inputs["source_sha256"])
    shared.check_manifest(inputs["input_sha256"])
    _require(_same(shared.environment(), inputs["backend"]), "final_verifier_current_backend_changed")
    for name in required:
        module = sys.modules.get(name[:-3].replace("/", "."))
        if module is not None:
            _require(Path(module.__file__).resolve() == shared.relative(name).resolve(), "final_foreign_verifier_import")
    return {"source_bytes_match": True, "input_bytes_match": True, "backend_matches": True,
        "loaded_checker_paths_match": True, "source_commit_label_authenticated": False}


def verify_handoff_record(record, governance, original, selected_origin, *, inputs, host):
    """Rebuild exact actual point/receipt chain; this never recreates ownership."""
    from .starship_final_handoff import FinalActualReturnHandoffPolicy
    from . import starship_restored_native_verifier as native
    from . import starship_restored_contract as contract
    from . import starship_sixdof as dyn
    required = {"schema", "policy", "origin", "source_record_sha256", "inventory", "snapshot", "coast_history",
        "initial_binding", "reservation_changes", "dispatch", "old_actor_suspended_permanently",
        "actual_state_assigned", "plant_advanced", "return_controller_started", "forecast_state_used",
        "completed_flight", "source_provenance_authenticated", "host_transcript_binding_required",
        "old_certificate_admitted", "qualification_refreshed", "mission_authority_granted",
        "dispatch_authorized", "physical_execution", "handoff_sha256"}
    _require(type(record) is dict and set(record) == required
        and record["schema"] == "missionos.starship_final_actual_dispatch_handoff.v1"
        and _hash(record["handoff_sha256"])
        and record["handoff_sha256"] == shared.digest({key: value for key, value in record.items() if key != "handoff_sha256"}),
        "final_handoff_raw_preimage_or_hash_changed")
    expected_policy = FinalActualReturnHandoffPolicy(inputs["plan_sha256"], inputs["source_sha256"]).record()
    _require(_same(record["policy"], expected_policy) and _same(record["origin"], selected_origin.record())
        and record["source_record_sha256"] == selected_origin.source_record_sha256
        and _same(record["inventory"], shared.parse(selected_origin.source_record_bytes())["inventory"])
        and _same(record, governance["final_handoff"])
        and _same(record["dispatch"], governance["dispatch"]), "final_handoff_origin_inventory_or_dispatch_reassigned")
    false_fields = {"actual_state_assigned", "plant_advanced", "return_controller_started", "forecast_state_used",
        "completed_flight", "source_provenance_authenticated", "old_certificate_admitted", "qualification_refreshed",
        "mission_authority_granted", "dispatch_authorized", "physical_execution"}
    _require(all(record[name] is False for name in false_fields)
        and record["old_actor_suspended_permanently"] is record["host_transcript_binding_required"] is True,
        "final_handoff_claim_boundary_changed")
    snapshot = record["snapshot"]
    _require(set(snapshot) == {"state", "vehicle", "phase", "binding", "return_controller", "completed"}
        and snapshot["completed"] is False and _same(snapshot, record["dispatch"]["actual_snapshot"]),
        "final_handoff_snapshot_shape_or_dispatch_changed")
    state = native._snapshot(snapshot, original, binding=snapshot["binding"])
    binding = snapshot["binding"]
    _require(0 <= state["time_s"]-binding["return_time_s"] <= binding["maximum_start_clock_slip_s"]+1e-7,
        "final_handoff_not_first_due_reserved_tick")
    initial, changes = record["initial_binding"], record["reservation_changes"]
    origin_row = original.record()
    _require(type(initial) is dict and initial.get("schema") == "missionos.starship_restored_return_request.v1"
        and initial.get("candidate_id") == "nominal" and initial.get("origin_sha256") == shared.digest(origin_row)
        and initial.get("source_record_sha256") == original.source_record_sha256
        and initial.get("source_sha256") == shared.digest(inputs["source_sha256"])
        and initial.get("profile_sha256") == origin_row["profile_sha256"]
        and initial.get("inventory_receipt_chain_sha256") == origin_row["inventory_receipt_chain_sha256"]
        and type(changes) is list and len(changes) <= 1,
        "final_handoff_initial_actual_binding_or_change_budget")
    prior_binding = initial
    for index, change in enumerate(changes):
        _require(type(change) is dict and set(change) == {"time_s", "state", "from_binding", "to_binding",
                "actual_state_assigned", "dispatch_authorized"}
            and _same(change["from_binding"], prior_binding)
            and change["actual_state_assigned"] is change["dispatch_authorized"] is False
            and change["time_s"] == change["state"]["time_s"] < state["time_s"]
            and governance["current_origin_changes"][index]["reservation_change_sha256"] == shared.digest(change),
            "final_handoff_actual_reservation_change_hash_or_continuity")
        prior_binding = change["to_binding"]
    _require(_same(prior_binding, binding), "final_handoff_selected_binding_lost_actual_changes")
    history = record["coast_history"]
    _require(type(history) is list and 1 <= len(history) <= 100000
        and _same(history[0], {"state": original.record()["state"], "retained_count": original.record()["retained_count"]})
        and _same(history[-1], {"state": state, "retained_count": original.record()["retained_count"]}),
        "final_handoff_actual_coast_history_endpoints_changed")
    craft = dyn.vehicle_from_dict(original.record()["vehicle"])
    outages = {row["engine_name"] for row in original.record()["ascent_settings"]["ascent"]["shutdowns"]
        if row["engine_name"].startswith("ship_")}
    prior = None
    for witness in history:
        _require(type(witness) is dict and set(witness) == {"state", "retained_count"}
            and type(witness["retained_count"]) is int and witness["retained_count"] == original.record()["retained_count"],
            "final_handoff_coast_inventory_changed")
        actual = contract._state(witness["state"], craft, outages)
        if prior is not None:
            _require(actual.time_s >= prior["time_s"] and actual.propellant_kg <= prior["propellant_kg"]+1e-6
                and (actual.time_s != prior["time_s"] or _same(witness["state"], prior)),
                "final_handoff_coast_clock_fuel_or_actuator_reset")
        prior = witness["state"]
    sequence = governance.get("final_handoff_telemetry_sequence")
    rows = host.get("telemetry")
    _require(type(sequence) is int and sequence > 0 and type(rows) is list and rows
        and rows[-1]["frame"]["sequence"] == sequence
        and _same(rows[-1]["frame"]["actual_state"], state)
        and rows[-1]["frame"]["vehicle_sha256"] == shared.digest(snapshot["vehicle"])
        and rows[-1]["frame"]["return_controller_started"] is False,
        "final_handoff_not_last_external_host_accepted_snapshot")
    return {"actual_snapshot": snapshot, "inventory": record["inventory"], "binding": binding,
        "handoff_sha256": shared.digest(record), "same_actual_state_and_actuators": True,
        "receipt_chain_preserved": True, "host_content_consistent": True,
        "source_authentication_established": False, "physical_reexecution": False}


def _forecast_host_projection(host, backend):
    """Check the final six-field host before a legacy five-field comparison.

    The restored forecast worker never recorded ``machine``. Its five existing
    fields still require exact equality; the complete final host is separately
    pinned to the registered backend. Do not invent a worker architecture or
    modify the original host transcript to conceal the schema difference.
    """
    keys = {"python", "platform", "machine", "numpy", "scipy", "executable_sha256"}
    _require(type(backend) is dict and set(backend) == keys
        and type(host.get("environment")) is dict and set(host["environment"]) == keys
        and _same(host["environment"], backend), "final_forecast_host_backend_changed")
    return {**host, "environment": {key: backend[key] for key in sorted(keys-{"machine"})}}



def _visual_deployment_partition(prefix, visual, *, inputs, host, rpc):
    """Validate the one final visual stop without pretending it was a model choice.

    Called only after the complete visual host/RPC/pixel/counter checks. The
    digest was captured by the executor at this actual terminal decision tick;
    the ordinary closed-inventory/state/order verifier still checks both rows.
    """
    from . import starship_final_visual as visual_module
    decisions = prefix["restored_prefix"]["decisions"]
    ordinary_origin = "external_callback_not_model_or_approval_proof"
    special = [row for row in decisions if row.get("origin") != ordinary_origin]
    if not special:
        _require(visual.get("withhold_deployment") is not True,
            "final_visual_stop_missing_actual_deployment_decision")
        return decisions, None
    _require(len(special) == 1 and len(decisions) == 2 and special[0] is decisions[-1],
        "final_visual_stop_not_unique_terminal_decision")
    row, summary = special[0], special[0]["summary"]
    now = summary["time_s"]
    _require(row.get("origin") == "final_visual_observation_withhold_not_approval_proof"
        and row.get("action") == "withhold_deployment"
        and row.get("final_visual_observation_sha256") == shared.digest(visual)
        and row.get("pending") is row.get("callback_error_type") is None
        and summary.get("released_count") == summary.get("retained_count") == 13
        and summary.get("sequencer_state") == "held"
        and summary.get("notice") == "Independent observation episode ended with further deployment withheld."
        and summary.get("decision_started_s") is summary.get("decision_deadline_s") is None
        and visual.get("withhold_deployment") is True and visual.get("pending_request") is None,
        "final_visual_stop_decision_evidence_binding")
    episode = visual["episode"]
    _verify_visual_epoch(episode, None, None, inputs["visual_policy"])
    closed = [event for event in visual["events"] if event.get("event") == "visual_episode_closed"]
    _require(len(closed) == 1 and closed[0].get("action") == "withhold_deployment"
        and closed[0].get("reason") == visual["closed_reason"]
        and closed[0].get("time_s") == now == prefix["final_state"]["time_s"]
        and episode["started_s"] <= now
        and closed[0].get("inventory_assigned") is closed[0].get("plant_state_assigned") is False,
        "final_visual_stop_closure_or_clock_changed")
    ship = [(index, tick) for index, tick in enumerate(prefix["concurrent_execution"]["tick_order"])
        if tick["body_id"] == "ship"]
    observed, excluded = _visual_observer_ticks(prefix, ship)
    _require(observed and excluded is None and observed[-1][2]["time_s"] == now
        and observed[-1][2]["released_count"] == 13,
        "final_visual_stop_not_actual_post_observer_terminal_tick")
    reason, rows = visual["closed_reason"], visual["results"]
    _require(reason in ("invalid_visual_result_no_retry", "accepted_visual_proposal"),
        "final_visual_stop_reason_requires_explicit_saved_evidence")
    _require(rows and rows[-1]["time_s"] == now, "final_visual_stop_missing_same_tick_result")
    last = rows[-1]
    if reason == "accepted_visual_proposal":
        _require(last["accepted"] is True and last["reply"]["result"]["action"] == "stop_deployment"
            and closed[0].get("basis") == ("ai" if inputs["mode"] == "live" else "fixture"),
            "final_visual_stop_not_accepted_stop_proposal")
    else:
        _require(last["accepted"] is False and last.get("rejection") == "invalid_or_unadmitted_visual_result"
            and closed[0].get("basis") == "rules", "final_visual_stop_not_rule_rejection")
        envelopes = [e for e in visual["requests"] if e["envelope_sha256"] == last["envelope_sha256"]]
        attempts = [a for a in host["attempts"] if a["envelope_sha256"] == last["envelope_sha256"]]
        _require(len(envelopes) == len(attempts) == 1, "final_visual_stop_missing_bound_host_attempt")
        envelope, response = envelopes[0], attempts[0]["response"]
        _require(envelope["request"]["snapshot_time_s"] <= now
            and now+inputs["visual_policy"]["apply_margin_s"] < episode["deadline_s"],
            "final_visual_rejection_outside_original_snapshot_deadline")
        exchange = rpc.get(envelope["request"]["request_id"])
        _require(exchange is not None and exchange["client"] is not None
            and exchange["client"]["expired"] is False
            and _same(exchange["client"]["delivered_result"], response)
            and response.get("schema") == visual_module.HOST_RESULT
            and all(response.get(key) == visual[key] for key in (
                "run_id", "plan_sha256", "source_sha256", "mode", "policy_sha256"))
            and response.get("envelope_sha256") == envelope["envelope_sha256"]
            and response.get("request_sha256") == envelope["request_sha256"]
            and response.get("result_sha256") == shared.digest(response["result"]),
            "final_visual_stop_rejection_not_delivered_bound_host")
        invalid = False
        try:
            visual_module.validate_result(response["result"], envelope["request"], inputs["mode"])
        except (ValueError, TypeError, KeyError):
            invalid = True
        _require(invalid, "final_visual_stop_claimed_rejection_of_valid_response")
    row_hash = shared.digest(row)
    def metadata(actual, count, action):
        _require(shared.digest(actual) == row_hash and count == 13 and action == "withhold_deployment",
            "final_visual_stop_partition_changed")
        return True
    return decisions[:-1], metadata


def verify_pre_dispatch(record, inputs, artifacts, host, *, visual_host, rpc):
    """Use v1 saved checks only for unchanged-vehicle launch/coast governance."""
    from . import starship_restored_native_verifier as native
    from . import starship_restored_verifier as physical
    from . import starship_restored_contract as contract
    from .starship_restored_integration import RestoredIntegrationPolicy
    from .starship_state_mission_metrics import _settings
    from .starship_booster_terminal_metrics import _settings as control_settings
    from .starship_final_prefix_verifier import verify_prefix_physical
    profile, source, policy = inputs["profile"], inputs["source_sha256"], RestoredIntegrationPolicy.from_record(inputs["integration_policy"])
    _require(type(artifacts) is dict and all(_hash(identity) and type(raw) is bytes
        and sha256(raw).hexdigest() == identity for identity, raw in artifacts.items())
        and sum(len(raw) for raw in artifacts.values()) <= MAXIMUM_ARTIFACT_BYTES,
        "final_external_artifact_map_hash_type_or_budget")
    profile_raw = artifacts.get(contract.PROFILE_SHA256)
    _require(type(profile_raw) is bytes and _same(shared.parse(profile_raw), profile),
        "final_external_original_profile_raw_missing_or_changed")
    native._review_binding(host, inputs["return_review_policy"])
    _require(host.get("schema") == native.HOST_SCHEMA and _same(host.get("context"), inputs["context"])
        and host.get("plan_sha256") == inputs["plan_sha256"]
        and host.get("source_sha256") == shared.digest(source) and host.get("mode") == inputs["mode"]
        and _same(host.get("integration_policy"), inputs["integration_policy"]),
        "final_restored_host_component_binding_changed")
    physical._integration_receipt(record, policy)
    settings, controls = _settings(inputs["ascent_settings"], profile), control_settings(inputs["control_settings"])
    reached, commands, outcome, booster_success, water = verify_prefix_physical(record["prefix"], profile,
        settings=settings, controls=controls, expected_settings=inputs["ascent_settings"],
        expected_site=inputs["site_catalog"]["sites"][1], expected_goal=inputs["goal"],
        expected_return_sites=inputs["site_catalog"], expected_prefix_horizon_s=7500.,
        expected_source_sha256=source, expected_plan_sha256=inputs["plan_sha256"],
        expected_context=inputs["context"], expected_mode=inputs["mode"])
    result = {"prefix_consistent": True, "measured_final_insertion_gate_reached": reached,
        "verified_ascent_command_samples": commands, "booster_contact_envelope_met": booster_success,
        "booster": water, "termination": outcome["termination"], "closed_inventory": False}
    if outcome["termination"] != "restored_deployment_handoff":
        _require(all(record.get(name) is None for name in ("governance", "handoff", "fault_harness", "sensor_return"))
            and record["prefix"]["restored_prefix"].get("closed") is False
            and not host.get("decision_host", {}).get("requests"), "final_failed_prefix_fabricated_later_work")
        return result
    original = physical._origin(record["prefix"], profile_raw, source)
    prefix = record["prefix"]
    inventory = prefix["restored_prefix"]["handoff_record"]["inventory"]
    result["visual"] = verify_visual(record["supplemental"]["visual"], inputs=inputs,
        host=visual_host, rpc=rpc, artifacts=artifacts, inventory=inventory, prefix=prefix)
    model_decisions, visual_metadata = _visual_deployment_partition(prefix,
        record["supplemental"]["visual"], inputs=inputs, host=visual_host, rpc=rpc)
    physical._decisions(prefix, prefix["restored_prefix"]["handoff_record"], profile,
        extra_metadata_validator=visual_metadata)
    governance, handoff = record["governance"], record["handoff"]
    refused = type(governance) is dict and governance.get("schema") == "missionos.starship_final_refused_governance.v1"
    _require(type(governance) is dict and governance.get("schema") in (
        "missionos.starship_final_pre_dispatch_governance.v1", "missionos.starship_final_refused_governance.v1")
        and governance.get("stage") == ("inhibited" if refused else "handed_off")
        and governance.get("completed_v1_flight") is False
        and _same(governance.get("integration_policy"), policy.record())
        and all(governance.get(name) is False for name in ("qualification_refreshed", "mission_authority_granted", "physical_execution")),
        "final_pre_dispatch_record_must_not_claim_completed_v1")
    retained = native._origin(governance["closed_origin"]["raw"], artifacts, profile_raw, source)
    _require(_same(retained.record(), original.record())
        and governance["closed_origin"]["origin_sha256"] == shared.digest(original.record()),
        "final_closed_origin_not_actual_launch_inventory")
    forecasts = native._forecast_batches(governance["forecast_batches"], artifacts, profile_raw, source, policy, original,
        _forecast_host_projection(host, inputs["backend"]))
    result["forecast_environment"] = {"final_host_all_six_fields_checked": True,
        "worker_recorded_fields_checked": ["executable_sha256", "numpy", "platform", "python", "scipy"],
        "forecast_worker_machine_recorded": False}
    if refused:
        _require(handoff is record["fault_harness"] is record["sensor_return"] is None
            and governance["dispatch"].get("gate_met") is False
            and governance["dispatch"].get("refusal_reason") == "dispatch_not_admitted",
            "final_governed_refusal_not_an_explicit_independent_admission_stop")
        owner = governance["final_refusal_owner"]
        _require(type(owner) is dict and owner.get("schema") == "missionos.starship_final_actual_refusal_owner.v1",
            "final_refusal_lost_actual_owner_preimages")
        binding_projection = {key: owner[key] for key in ("initial_binding", "binding", "reservation_changes")}
    else:
        _require(type(handoff) is dict, "final_closed_prefix_lost_actual_handoff")
        binding_projection = {"initial_binding": handoff["initial_binding"], "binding": handoff["snapshot"]["binding"],
            "reservation_changes": handoff["reservation_changes"]}
    operations = native._operations(inputs["operations"])
    initial, final, changes = native._changes(governance, binding_projection, original, artifacts, profile_raw, source, forecasts, operations)
    observations = native._telemetry(host, original, initial, final, changes, context=inputs["context"],
        plan_sha256=inputs["plan_sha256"], source=source, policy=policy, binding=governance["approval"]["binding"])
    native._snapshot_history(governance, host, original)
    bound, authority_confirmed, amendment = native._approval(governance, host, original, initial, final, changes,
        context=inputs["context"], plan_sha256=inputs["plan_sha256"], source=source, inhibited=refused, observations=observations)
    native._handshake(governance, host, context=inputs["context"], plan_sha256=inputs["plan_sha256"], source=source,
        original=original, initial=initial, policy=policy, binding=bound)
    exchanges, attempted, confirmed = native._judgments(governance["decision_client"], host["decision_host"],
        context=inputs["context"], plan_sha256=inputs["plan_sha256"], source=source, mode=inputs["mode"],
        expected_return_review_policy=inputs["return_review_policy"])
    deployment_native, used = native._deployment(governance, record["prefix"], exchanges, operations,
        profile=profile, source=source, scope_sha256=shared.digest({"context": inputs["context"],
            "plan_sha256": inputs["plan_sha256"], "source_sha256": source, "phase": "deployment"}),
        model_decisions=model_decisions)
    return_used, return_native, honored = native._return_decisions(governance, exchanges, original, forecasts,
        profile_raw, source, bound, operations, expected_return_context_projection=inputs["return_context_projection"])
    _require(used.isdisjoint(return_used) and used | return_used == set(exchanges),
        "final_pre_dispatch_unaccounted_or_repeated_native_request")
    dispatch = governance["dispatch"]
    _require(type(dispatch) is dict and type(dispatch.get("gate_met")) is bool, "final_dispatch_gate_missing")
    snapshot = dispatch["actual_snapshot"]
    state = native._snapshot(snapshot, original, binding=final)
    admission = None
    if dispatch["candidate"] is not None:
        candidate, notice, resources = native._candidate(dispatch["candidate"], forecasts, profile_raw, source, original)
        native._operations_row(candidate.record(), state["time_s"], operations, original)
        admission = native._saved_admission(candidate, snapshot, original, notice, resources, "dispatch")
        _require(_same(dispatch["admission"], admission)
            and candidate.record()["proposal"]["candidate_id"] == final["candidate_id"],
            "final_due_dispatch_admission_not_recomputed_or_other_candidate")
    else:
        _require(dispatch["admission"] is None, "final_missing_candidate_claimed_admission")
    result.update(closed_inventory=True, deployment_native=deployment_native,
        return_native=return_native, honored_native_return_choice=honored,
        operator_amendment_consistency_verified=amendment, hosted_model_attempts=attempted,
        confirmed_native_inferences=confirmed, return_candidate_id=final["candidate_id"],
        retained_count=original.record()["retained_count"], released_count=original.record()["released_count"],
        reservation_change_count=len(changes), governed_no_handoff=refused,
        inventory=shared.parse(original.source_record_bytes())["inventory"])
    if refused:
        _require(not dispatch["gate_met"] and (admission is not None and admission["accepted"] is False
            or admission is None and authority_confirmed is False),
            "final_refusal_without_independent_admission_or_unconfirmed_scope")
        result["refusal"] = verify_refusal_owner(owner, governance, original, initial, final,
            inputs=inputs, settings=settings, host=host)
        result["dispatch_admission_checked"] = admission is not None
        if inputs["return_review_policy"] is not None:
            result["return_review"] = native._review_verification(exchanges, inputs["return_review_policy"])
        return result
    _require(dispatch["gate_met"] and dispatch["refusal_reason"] is None and authority_confirmed
        and admission is not None and admission["accepted"] is True,
        "final_handoff_without_accepted_due_dispatch_and_scope")
    selected = original if not changes else native._origin(changes[-1]["origin"], artifacts, profile_raw, source)
    actual = verify_handoff_record(handoff, governance, original, selected, inputs=inputs, host=host)
    _require(_same(actual["binding"], final), "final_handoff_lost_selected_binding")
    result.update(handoff=actual, dispatch_admission_checked=True)
    if inputs["return_review_policy"] is not None:
        result["return_review"] = native._review_verification(exchanges, inputs["return_review_policy"])
    return result


def verify_refusal_owner(owner, governance, original, initial, final, *, inputs, settings, host):
    """Check the actual stopped coast; never construct a completed v1 flight."""
    from . import starship_restored_verifier as physical
    from . import starship_restored_contract as contract
    from . import starship_sixdof as dyn
    _require(governance.get("schema") == "missionos.starship_final_refused_governance.v1"
        and governance.get("stage") == "inhibited" and governance.get("completed_v1_flight") is False
        and governance["dispatch"].get("gate_met") is False
        and governance["dispatch"].get("refusal_reason") == "dispatch_not_admitted",
        "final_refusal_owner_unknown_or_infrastructure_stop")
    _require(type(owner) is dict and set(owner) == {"schema", "initial_binding", "binding", "reservation_changes",
        "coast_history", "actual_snapshot", "continuation", "completed_v1_flight", "contact", "new_execution_authority_granted"}
        and owner["schema"] == "missionos.starship_final_actual_refusal_owner.v1"
        and owner["completed_v1_flight"] is owner["new_execution_authority_granted"] is False and owner["contact"] is None
        and _same(owner["initial_binding"], initial) and _same(owner["binding"], final),
        "final_refusal_owner_scope_or_claims_changed")
    snapshot, dispatch = owner["actual_snapshot"], governance["dispatch"]["actual_snapshot"]
    _require(type(snapshot) is dict and set(snapshot) == set(dispatch)
        and snapshot["completed"] is True and dispatch["completed"] is False
        and all(_same(snapshot[key], dispatch[key]) for key in snapshot if key != "completed")
        and snapshot["return_controller"] is None and snapshot["phase"] == "orbital_coast",
        "final_refusal_owner_not_exact_actual_unstarted_stop")
    continuation = owner["continuation"]
    _require(_same(continuation["initial_binding"], initial) and _same(continuation["binding"], final)
        and _same(continuation["reservation_changes"], owner["reservation_changes"])
        and _same(continuation["final_state"], snapshot["state"])
        and continuation["outcome"]["termination"] == "return_inhibited_by_governance"
        and continuation["return_start_state"] is continuation["return_controller"] is None
        and continuation["outcome"]["contact_receipt"] is None
        and not any(row["event"] == "return_requested" for row in continuation["events"])
        and any(row["event"] == "return_inhibited_by_governance"
            and row.get("reason") == governance["dispatch"]["refusal_reason"] for row in continuation["events"]),
        "final_refusal_invented_ignition_or_contact")
    history = owner["coast_history"]
    _require(type(history) is list and history
        and _same(history[0], {"state": original.record()["state"], "retained_count": original.record()["retained_count"]})
        and _same(history[-1], {"state": snapshot["state"], "retained_count": original.record()["retained_count"]}),
        "final_refusal_actual_coast_endpoints_changed")
    craft = dyn.vehicle_from_dict(original.record()["vehicle"])
    outages = {row["engine_name"] for row in original.record()["ascent_settings"]["ascent"]["shutdowns"]
        if row["engine_name"].startswith("ship_")}
    prior = None
    for row in history:
        state = contract._state(row["state"], craft, outages)
        _require(set(row) == {"state", "retained_count"} and row["retained_count"] == original.record()["retained_count"]
            and (prior is None or state.time_s >= prior["time_s"]
                and state.propellant_kg <= prior["propellant_kg"]+1e-7), "final_refusal_actual_coast_reset")
        prior = row["state"]
    _require(any(_same(row["frame"]["actual_state"], snapshot["state"])
        and row["frame"]["vehicle_sha256"] == shared.digest(snapshot["vehicle"])
        and row["frame"]["return_controller_started"] is False for row in host["telemetry"]),
        "final_refusal_actual_stop_not_host_observed")
    contact = physical._return_physical(continuation, original, inputs["profile"], settings,
        float(final["maximum_simulation_time_s"]), initial_binding=initial, final_binding=final,
        reservation_changes=owner["reservation_changes"], allow_governance_stop=True)
    _require(contact["contact_observed"] is contact["contact_envelope_met"] is False,
        "final_refusal_promoted_to_recovery")
    return {"saved_unstarted_actual_coast_consistent": True, "contact": None,
        "ship_contact_limits_met": False, "completed_v1_flight": False,
        "refusal_reason": governance["dispatch"]["refusal_reason"], "physical_reexecution": False}


def _session_epoch(session, maximum_s):
    start, end = session["started_wall_s"], session["wall_deadline_s"]
    _require(_number(start) and _number(end) and 0 <= start < end <= start+maximum_s+1e-7,
        "final_task_original_epoch_or_budget")
    if session["status"] in ("accepted", "received"):
        _require(_number(session.get("received_wall_s")) and start <= session["received_wall_s"] < end,
            "final_task_late_result_marked_accepted")


def verify_rpc(client, host, inputs):
    """Join retained pipe frames; expiry never turns a late answer into work."""
    from . import starship_final_protocol as protocol
    binding = {"plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"])}
    _require(type(client) is dict and client.get("schema") == "missionos.starship_final_rpc_client.v1"
        and type(host) is dict and host.get("schema") == "missionos.starship_final_rpc_host.v1"
        and all(client.get(k) == host.get(k) == value for k, value in binding.items())
        and client.get("provider_calls_by_worker") == 0
        and client.get("execution_permission_granted") is host.get("execution_permission_granted") is False
        and type(client.get("completed")) is list and type(host.get("sessions")) is list,
        "final_rpc_saved_binding_or_authority")
    sessions, completed, used = host["sessions"], client["completed"], set()
    _require(len(sessions) <= 256 and len(completed) <= len(sessions), "final_rpc_saved_session_budget")
    frames = {}
    for index, session in enumerate(sessions, 1):
        frame = session["frame"]
        value = protocol.validate_request(frame["request"])
        _require(type(frame) is dict and set(frame) == {"schema", *binding, "sequence", "request", "request_sha256",
            "started_wall_s", "wall_deadline_s"} and frame["schema"] == protocol.REQUEST
            and all(frame[k] == v for k, v in binding.items()) and frame["sequence"] == index
            and frame["request_sha256"] == shared.digest(value)
            and value["request_id"] not in frames and _number(frame["started_wall_s"])
            and frame["wall_deadline_s"] == frame["started_wall_s"]+value["deadline_s"]-value["time_s"],
            "final_rpc_saved_request_hash_sequence_or_epoch")
        frames[value["request_id"]] = session
    for row in completed:
        frame = row["frame"]
        request_id = frame["request"]["request_id"]
        _require(request_id in frames and request_id not in used and _same(frame, frames[request_id]["frame"])
            and type(row["expired"]) is bool and row["proposal_only"] is True
            and row["execution_permission_granted"] is False, "final_rpc_saved_client_host_reassignment")
        used.add(request_id)
        response, session = row["response"], frames[request_id]
        if response is not None:
            _require(type(response) is dict and set(response) == {"schema", *binding, "sequence", "request_sha256", "result", "result_sha256"}
                and response["schema"] == protocol.RESPONSE and all(response[k] == v for k, v in binding.items())
                and response["sequence"] == frame["sequence"] and response["request_sha256"] == frame["request_sha256"]
                and response["result_sha256"] == shared.digest(response["result"])
                and _same(response["result"], session.get("delivered_result")), "final_rpc_saved_response_changed")
        _require(row["delivered_result"] is None if row["expired"] else response is not None
            and _same(row["delivered_result"], response["result"]) and session.get("expired") is False,
            "final_rpc_saved_late_response_adopted")
    pending = client.get("pending")
    _require(pending is None or type(pending) is dict and pending["frame"]["request"]["request_id"] in frames,
        "final_rpc_saved_pending_unbound")
    return {request_id: {"host": session, "client": next((row for row in completed
            if row["frame"]["request"]["request_id"] == request_id), None)} for request_id, session in frames.items()}


def _actual_rollout_checks(rollout, nominal, actual, initial_state):
    """Check saved ordered control routing without claiming re-propagation."""
    from . import starship_ship_capability_observation as obs
    from . import starship_sixdof as dyn
    from .starship_final_sensor_prediction import CONTACT_LIMITS
    _require(rollout.get("runtime_admission") is rollout.get("physical_execution") is False
        and rollout.get("model_calls") == 0 and rollout.get("fault_domain_status") == "unverified"
        and _same(rollout["initial_state"], initial_state), "final_sensor_rollout_initial_or_claims_changed")
    # Reuse the algebraic checker but bind its nominal vehicle to the actual
    # retained geometry; no profile-created substitute may hide a mass delta.
    nominal_limits = [e.max_thrust_n for e in nominal.engines]
    actual_limits = [e.max_thrust_n for e in actual.engines]
    _require(rollout["nominal_engine_limits_n"] == nominal_limits
        and rollout["plant_engine_limits_n"] == actual_limits,
        "final_sensor_rollout_vehicle_capability_reassigned")
    verified = obs.verify_record(rollout, CONTACT_LIMITS)
    # Physical and budget failures remain negative observations. All other
    # checks must still hold, so a truthful failure need not become forged data.
    outcome_checks = {"no_numerical_or_budget_failure", "terminal_contact_retained", "contact_kinematics", "contact_is_final_state"}
    _require(all(value for key, value in verified["checks"].items() if key not in outcome_checks),
        "final_sensor_rollout_saved_arithmetic_or_audit_invalid")
    _require(verified.get("error") is None, "final_sensor_rollout_missing_raw_fields")
    rows = rollout["control_capability_audit"]["all_boundary_calls"]
    _require(type(rows) is list and rows and len(rows) <= 1000000, "final_sensor_rollout_audit_missing")
    position, steps = 0, 0
    while position < len(rows):
        block = rows[position:position+2]
        _require([r["boundary"] for r in block] == ["update", "capability_profile"], "final_sensor_rollout_update_route")
        position += 2
        if position < len(rows) and rows[position]["boundary"] == "command":
            block += rows[position:position+4]
            _require([r["boundary"] for r in block[2:]] == ["command", "capability_profile", "tvc_control", "plant"],
                "final_sensor_rollout_command_route")
            position += 4
            steps += 1
        _require(all(r["time_s"] == block[0]["time_s"] for r in block), "final_sensor_rollout_step_time_changed")
    _require(steps == rollout["outcome"]["integration_steps"] and rows[0]["time_s"] == initial_state["time_s"]
        and all(a["time_s"] <= b["time_s"] for a, b in zip(rows, rows[1:])), "final_sensor_rollout_step_census")
    unavailable = {i for i, engine in enumerate(initial_state["engine_states"]) if not engine["available"]}
    for sample in rollout["samples"]:
        state = {name: sample[name] for name in initial_state}
        restored = dyn.state_from_dict(state)
        _require(_same(asdict(restored), state) and len(restored.engine_states) == len(actual.engines)
            and len(restored.flap_angles_rad) == len(actual.aero_panels)
            and all(not sample["engine_states"][i]["available"] for i in unavailable),
            "final_sensor_rollout_actuator_reset_or_missing")
    _require(_same({name: rollout["samples"][-1][name] for name in initial_state}, rollout["final_state"]),
        "final_sensor_rollout_final_state_changed")
    return verified


def expanded_guard_bundle(session, sensor):
    """Restore one exact checker preimage from its finite saved batch link."""
    saved = session["bundle"]
    _require(type(saved) is dict and "forecasts" not in saved
        and _hash(saved.get("forecasts_sha256")) and _hash(saved.get("forecast_batch_snapshot_id")),
        "final_guard_compact_batch_link_required")
    matches = [row for row in sensor["forecast_batches"]
        if row["snapshot"]["snapshot_id"] == saved["forecast_batch_snapshot_id"]]
    _require(len(matches) == 1 and matches[0]["forecasts"] is not None
        and saved["forecasts_sha256"] == shared.digest(matches[0]["forecasts"])
        and _same(saved["snapshot"], matches[0]["snapshot"]), "final_guard_saved_forecasts_reassigned")
    return {**{key: value for key, value in saved.items() if key not in ("forecasts_sha256", "forecast_batch_snapshot_id")},
        "forecasts": matches[0]["forecasts"]}



def _sensor_integration_binding(value, inputs):
    """Bind explicit v2 wires to the frozen plan; retain historical v1/N2 only."""
    from .starship_restored_integration import RestoredIntegrationPolicy
    from . import starship_final_sensor_execution as execution
    policy = RestoredIntegrationPolicy.from_record(inputs["integration_policy"])
    explicit = type(value) is dict and value.get("schema") == execution.SCHEMA_V2
    if explicit:
        _require(_same(value.get("integration_policy"), policy.record()),
            "final_sensor_explicit_integration_policy_changed")
        return {"integration_policy": policy.record()}
    _require(type(value) is dict and value.get("schema") == execution.SCHEMA
        and "integration_policy" not in value and policy == RestoredIntegrationPolicy(2),
        "final_sensor_legacy_wire_requires_legacy_N2_policy")
    return {}


def verify_sensor_suffix(value, *, inputs, handoff, harness, host):
    """Saved actual continuation, numerical observations and hosted decisions.

    Source/hash consistency is not provenance authentication. The outer caller
    separately pins the raw host records and numerical process-task artifacts.
    """
    from . import starship_final_sensor_execution as execution
    from . import starship_final_sensor_prediction as prediction
    from . import starship_final_sensor_guard as guard
    from . import starship_final_faults as faults
    from . import starship_ship_capability_observation as obs
    from . import starship_ship_sensor_observation as sensors
    from . import starship_sixdof as dyn
    from ..intelligence.starship_final_sensor_agent import verify_saved_session, SCHEMA as AGENT_SCHEMA
    from .starship_ship_fault_screen import AvailableCapacityController
    required = {"schema", "run_id", "profile", "source_sha256", "backend", "policy", "scope_sha256", "handoff_record",
        "handoff_sha256", "inventory", "inventory_receipt_chain_sha256", "fault_harness", "initial_state", "actual_vehicle",
        "nominal_vehicle", "initial_controller", "final_controller", "accelerometer_telemetry", "tracking_source_rows",
        "sensor_source_rows", "actual_execution_trace", "detection", "tracking_delivery", "complete_estimate", "tracking_estimate", "complete_snapshot",
        "decision_sessions", "forecast_batches", "guard_sessions", "application", "fallback_ledger", "control_stage",
        "application_witness", "declined_work", "nonintervention_regression", "rollout", "claim_limits"}
    integration_fields = _sensor_integration_binding(value, inputs)
    required |= set(integration_fields)
    _require(type(value) is dict and set(value) == required
        and value["schema"] == (execution.SCHEMA_V2 if integration_fields else execution.SCHEMA)
        and value["run_id"] == inputs["context"]["run_id"] and _same(value["profile"], inputs["profile"])
        and _same(value["source_sha256"], inputs["source_sha256"])
        and _same(value["backend"], execution.numerical_backend(inputs["backend"]))
        and _same(value["policy"], inputs["sensor_policy"]), "final_sensor_saved_context_changed")
    _require(_same(value["handoff_record"], handoff) and value["handoff_sha256"] == shared.digest(handoff)
        and _same(value["inventory"], handoff["inventory"])
        and value["inventory_receipt_chain_sha256"] == shared.digest(handoff["inventory"]["release_receipts"])
        and _same(value["fault_harness"], harness) and _same(harness["selection"], inputs["fault_selection"])
        and _same(value["initial_state"], handoff["snapshot"]["state"]), "final_sensor_actual_handoff_or_inventory_changed")
    actual = dyn.vehicle_from_dict(value["actual_vehicle"])
    faults.verify_injection(harness, handoff_record=handoff, actual_state=dyn.state_from_dict(value["initial_state"]),
        before_vehicle=dyn.vehicle_from_dict(handoff["snapshot"]["vehicle"]), after_vehicle=actual)
    nominal = prediction.nominal_from_actual(actual, inputs["profile"], value["inventory"]["retained_count"])
    _require(_same(value["nominal_vehicle"], asdict(nominal)), "final_sensor_nominal_vehicle_reassigned")
    initial_controller = AvailableCapacityController.from_dict(value["initial_controller"])
    final_controller = AvailableCapacityController.from_dict(value["final_controller"])
    model = obs.model_vehicle(nominal, 1.)
    fresh = obs.SwitchingController(nominal, model, inputs["profile"], handoff["snapshot"]["binding"]["return_time_s"],
        value["inventory"]["released_count"], obs.SwitchAudit(nominal, model, actual))
    _require(_same(initial_controller.to_dict(), value["initial_controller"])
        and _same(fresh.controller.to_dict(), value["initial_controller"])
        and _same(final_controller.to_dict(), value["final_controller"])
        and initial_controller.return_time_s == handoff["snapshot"]["binding"]["return_time_s"]
        and initial_controller.released_count == final_controller.released_count == value["inventory"]["released_count"],
        "final_sensor_controller_history_or_inventory_changed")
    scope = shared.digest({"run_id": value["run_id"], "source_sha256": value["source_sha256"], "backend": value["backend"],
        "policy": value["policy"], **integration_fields,
        "handoff_sha256": shared.digest(handoff), "profile": value["profile"]})
    _require(value["scope_sha256"] == scope and value["claim_limits"] == {"model_calls_by_worker": 0,
        "native_judgments_require_external_host_transcript": True, "independent_checker_requires_external_process_transcript": True,
        "physical_execution": False, "qualification_refreshed": False, "new_cohort_application_boundary_and_wall_pacing": True},
        "final_sensor_scope_or_claims_changed")
    physical = _actual_rollout_checks(value["rollout"], nominal, actual, value["initial_state"])
    trace = value["actual_execution_trace"]
    _require(type(trace) is list and trace and len(trace) <= 500000
        and _same(trace[0]["state"], value["initial_state"])
        and _same(trace[-1]["state"], value["rollout"]["final_state"]), "final_sensor_actual_trace_endpoints")
    traced, prior = set(), None
    for index, row in enumerate(trace, 1):
        restored_state = dyn.state_from_dict(row["state"])
        _require(type(row) is dict and set(row) == {"sequence", "boundary", "phase", "state", "vehicle_sha256"}
            and row["sequence"] == index and row["vehicle_sha256"] == shared.digest(value["actual_vehicle"])
            and type(row["boundary"]) is str and row["boundary"] and type(row["phase"]) is str and row["phase"]
            and _same(asdict(restored_state), row["state"])
            and len(restored_state.engine_states) == len(actual.engines)
            and len(restored_state.flap_angles_rad) == len(actual.aero_panels)
            and abs(sum(x*x for x in restored_state.q_body_to_eci)-1.) < 1e-8
            and all(abs(engine.gimbal_x_rad) <= spec.max_gimbal_rad+1e-8
                and abs(engine.gimbal_y_rad) <= spec.max_gimbal_rad+1e-8
                for engine, spec in zip(restored_state.engine_states, actual.engines))
            and all(abs(angle) <= spec.max_deflection_rad+1e-8
                for angle, spec in zip(restored_state.flap_angles_rad, actual.aero_panels)),
            "final_sensor_actual_trace_shape")
        if prior is not None:
            _require(row["state"]["time_s"] >= prior["time_s"]
                and row["state"]["propellant_kg"] <= prior["propellant_kg"]+1e-7
                and all(current["available"] is False for current, previous in
                    zip(row["state"]["engine_states"], prior["engine_states"]) if previous["available"] is False),
                "final_sensor_actual_trace_time_or_fuel_reset")
        traced.add(shared.digest(row["state"]))
        prior = row["state"]
    witnesses = value["sensor_source_rows"]
    _require(type(witnesses) is list and len(witnesses) <= value["rollout"]["outcome"]["integration_steps"],
        "final_sensor_raw_burn_witness_missing")
    actual_burn_rows = [{"state": row["state"], "phase": row["phase"]} for row in trace
        if row["boundary"] == "before_finite_actual_plant_step" and row["phase"] == "deorbit_burn"]
    _require(_same(witnesses, actual_burn_rows), "final_sensor_incomplete_actual_burn_observation_coverage")
    truth, tracking, previous = [], [], -1.
    plant_times = {row["time_s"] for row in value["rollout"]["control_capability_audit"]["all_boundary_calls"] if row["boundary"] == "plant"}
    for row in witnesses:
        state = dyn.state_from_dict(row["state"])
        _require(set(row) == {"state", "phase"} and row["phase"] == "deorbit_burn"
            and previous < state.time_s in plant_times and _same(asdict(state), row["state"])
            and shared.digest(row["state"]) in traced,
            "final_sensor_burn_witness_not_actual_ordered_step")
        previous = state.time_s
        truth.append(obs.specific_force_row(state, actual, row["phase"]))
        tracking.append(sensors.tracking_source_row(state, row["phase"]))
    gain = .8 if inputs["fault_selection"]["selected_family"] == "return_sensor_gain_80" else 1.
    measured = sensors.measure_accelerometer(truth, 0, value["policy"]["sensor_seed"], gain=gain)
    _require(_same(measured, value["accelerometer_telemetry"]) and _same(tracking, value["tracking_source_rows"]),
        "final_sensor_observation_truth_or_rng_reassigned")
    found = None
    for index, row in enumerate(witnesses, 1):
        candidate = obs.estimate(obs.prepare_telemetry(measured[:index], nominal), row["state"]["time_s"])
        if candidate["anomaly"]:
            found = candidate
            break
    _require(_same(found, value["detection"]), "final_sensor_detection_not_first_numeric_anomaly")
    snapshots = []
    def capture(snapshot):
        state, vehicle, history = prediction.validate_snapshot(snapshot)
        _require(_same(prediction.snapshot_integration_fields(snapshot), integration_fields),
            "final_sensor_capture_integration_policy_changed")
        _require(snapshot["run_id"] == value["run_id"] and snapshot["handoff_sha256"] == shared.digest(handoff)
            and _same(snapshot["inventory"], value["inventory"]) and _same(snapshot["actual_vehicle"], value["actual_vehicle"])
            and _same(snapshot["profile"], inputs["profile"]) and vehicle == nominal
            and state.time_s in plant_times and shared.digest(snapshot["state"]) in traced,
            "final_sensor_capture_reassigned_or_not_control_boundary")
        snapshots.append(snapshot)
        return state, history
    complete = value["complete_snapshot"]
    if complete is not None:
        state, _ = capture(complete)
        _require(state.time_s == complete["complete_time_s"]
            and math.isclose(complete["complete_time_s"], complete["cutoff_time_s"]+2., rel_tol=0., abs_tol=1e-8)
            and _same(value["complete_estimate"], obs.estimate(obs.prepare_telemetry(measured, nominal), state.time_s)),
            "final_sensor_complete_estimate_or_epoch_changed")
        cutoffs = [row for row in value["rollout"]["events"] if row["event"] == "deorbit_cutoff_command"]
        _require(len(cutoffs) == 1 and cutoffs[0]["time_s"] == complete["cutoff_time_s"]
            and all(row["state"]["time_s"] <= complete["cutoff_time_s"] for row in witnesses),
            "final_sensor_complete_epoch_not_actual_cutoff_event")
    _require(type(host) is dict and set(host) == {"schema", "mode", "sessions", "automatic_retry", "maximum_instance_calls", "execution_permission_granted"}
        and host["schema"] == AGENT_SCHEMA and host["mode"] == inputs["mode"] and host["automatic_retry"] is False
        and host["maximum_instance_calls"] is None and host["execution_permission_granted"] is False,
        "final_sensor_external_host_component_required")
    host_by_request = {}
    for session in host["sessions"]:
        request_id = session["request"]["request_id"]
        _require(request_id not in host_by_request, "final_sensor_host_request_replayed")
        host_by_request[request_id] = session
    decisions, native, fixtures, delegated, accepted = value["decision_sessions"], 0, 0, 0, []
    _require(type(decisions) is list and len(decisions) <= 2+value["policy"]["maximum_reconsiderations"],
        "final_sensor_decision_budget")
    first_final_epoch = None
    for index, session in enumerate(decisions):
        request = execution.validate_public_request(session["request"])
        _session_epoch(session, value["policy"]["initial_decision_s"] if request["stage"] == "initial" else value["policy"]["final_decision_s"])
        state, _ = capture(session["request_snapshot"])
        _require(session["request_sha256"] == shared.digest(request) and request["scope_sha256"] == scope
            and request["run_id"] == value["run_id"] and request["issued_at_s"] == state.time_s
            and request["stage"] == ("initial" if index == 0 else "final")
            and complete is not None and request["deadline_s"] <= complete["application_time_s"]-2.,
            "final_sensor_decision_capture_epoch_or_scope_changed")
        if request["stage"] == "final":
            if first_final_epoch is None:
                first_final_epoch = (session["wall_deadline_s"], request["deadline_s"])
            _require(session["wall_deadline_s"] <= first_final_epoch[0]+1e-7
                and request["deadline_s"] <= first_final_epoch[1]+1e-7,
                "final_sensor_reconsideration_renewed_original_epoch")
        estimate = obs.estimate(obs.prepare_telemetry(measured, nominal), state.time_s)
        facts = request["facts"]
        expected = {"time_s": state.time_s, "altitude_m": dyn.observe(state, nominal)["altitude_m"],
            "fuel_gauge_kg": obs.observation(state, nominal, "complete_burn")["fuel_gauge_kg"],
            "accel_factor": estimate["factor_estimate"], "accel_band_low": estimate["band"][0],
            "accel_band_high": estimate["band"][1], "accel_samples": estimate["accepted_samples"],
            "accel_anomaly": estimate["anomaly"], "application_time_s": complete["application_time_s"],
            "tracking_delay_s": sensors.TRACKING["request_latency_s"], "tracking_requested": request["stage"] == "final"}
        _require(all(facts.get(key) == val for key, val in expected.items()), "final_sensor_public_numeric_projection_changed")
        if request["stage"] == "initial":
            _require(request["allowed_actions"] == ([execution.CONTINUE, execution.TRACK] if estimate["anomaly"] else [execution.CONTINUE]),
                "final_sensor_initial_allowed_actions_changed")
        matched = host_by_request.get(request["request_id"])
        if matched is not None:
            response = matched["result"]
            checked = verify_saved_session(matched, request, response, inputs["mode"])
            if session["response"] is not None:
                _require(_same(session["response"], response), "final_sensor_worker_host_response_reassigned")
        else:
            _require(session["response"] is None and session["status"] in ("submit_error", "expired", "infrastructure_error"),
                "final_sensor_accepted_without_host_transcript")
            checked = {"valid_judgment": False, "native_judgments": 0, "fixture_judgments": 0, "explicit_delegation": False}
        if session["status"] == "accepted":
            _require(checked["valid_judgment"] and session["accepted_action"] == session["response"]["action"],
                "final_sensor_accepted_action_not_host_judgment")
            accepted.append(session)
            native += checked["native_judgments"]
            fixtures += checked["fixture_judgments"]
            delegated += checked["explicit_delegation"]
        else:
            _require(session["accepted_action"] is None, "final_sensor_unavailable_response_adopted")
    _require(set(host_by_request) <= {session["request"]["request_id"] for session in decisions},
        "final_sensor_unaccounted_host_attempt")
    delivery = value["tracking_delivery"]
    tracks = [session for session in accepted if session["accepted_action"] == execution.TRACK]
    continued = bool(accepted and accepted[0]["request"]["stage"] == "initial"
        and accepted[0]["accepted_action"] == execution.CONTINUE)
    declined = bool(continued and value["complete_estimate"]["anomaly"])
    _require(value["declined_work"] is declined
        and value["nonintervention_regression"] is bool(continued and not declined),
        "final_sensor_declined_work_or_nonintervention_mislabelled")
    if delivery is not None:
        _require(len(tracks) == 1 and _same(delivery, sensors.request_tracking(
            sensors.measure_tracking(tracking, value["policy"]["sensor_seed"]), delivery["requested_at_s"], shared.digest(tracking)))
            and tracks[0]["request"]["issued_at_s"] <= delivery["requested_at_s"]
                < tracks[0]["request"]["deadline_s"], "final_sensor_tracking_without_actual_request_or_raw_source")
    else:
        _require(not tracks and not value["forecast_batches"] and not value["guard_sessions"],
            "final_sensor_missing_requested_tracking")
    for batch in value["forecast_batches"]:
        _session_epoch(batch, value["policy"]["forecast_s"])
        _require("response" not in batch and "response_sha256" in batch
            and (batch["response_sha256"] is None or _hash(batch["response_sha256"]))
            and (batch["status"] != "received" or batch["forecasts"] is not None),
            "final_sensor_compact_forecast_callback_status")
        state, _ = capture(batch["snapshot"])
        _require(delivery is not None and state.time_s >= delivery["received_at_s"], "final_sensor_forecast_before_tracking_delivery")
        accel = obs.estimate(obs.prepare_telemetry(measured, nominal), state.time_s)
        tracking_estimate = sensors.estimate_tracking(delivery, nominal, state.time_s)
        _require(_same(batch["accelerometer_estimate"], accel) and _same(batch["sampling_estimate"], tracking_estimate),
            "final_sensor_forecast_estimates_reassigned")
        points = prediction._estimate(tracking_estimate, state.time_s)
        jobs = [{"snapshot": batch["snapshot"], "action": action, "sample_label": label,
            "sampled_factor": points[label], "accelerometer_estimate": accel, "sampling_estimate": tracking_estimate,
            "source_sha256": value["source_sha256"], "backend": value["backend"],
            **integration_fields, "timeout_s": min(180., value["policy"]["forecast_s"])}
            for action in prediction.ACTIONS for label in prediction.POINT_LABELS]
        _require(_same(batch["jobs"], jobs), "final_sensor_forecast_jobs_changed")
        if batch["forecasts"] is not None:
            prediction.forecast_preview(batch["forecasts"], batch["snapshot"])
            _require("response" not in batch and batch["response_sha256"] == shared.digest(batch["forecasts"]),
                "final_sensor_forecast_callback_response_changed")
    guard_receipts, guard_decisions = [], set()
    for session in value["guard_sessions"]:
        _session_epoch(session, value["policy"]["guard_s"])
        bundle, authority = expanded_guard_bundle(session, value), session["authority"]
        if integration_fields:
            _require(_same(bundle.get("integration_policy"), integration_fields["integration_policy"])
                and _same(authority.get("integration_policy"), integration_fields["integration_policy"]),
                "final_sensor_guard_integration_policy_changed")
        else:
            _require("integration_policy" not in bundle and "integration_policy" not in authority,
                "final_sensor_legacy_guard_cannot_add_policy")
        _require(bundle["decision"]["decision_id"] not in guard_decisions,
            "final_sensor_guard_repeated_same_proposal")
        guard_decisions.add(bundle["decision"]["decision_id"])
        _require(_same(bundle["accelerometer_telemetry"], measured) and _same(bundle["tracking_delivery"], delivery)
            and _same(authority["source_sha256"], value["source_sha256"]) and authority["scope_sha256"] == scope
            and authority["mode"] == inputs["mode"] and authority["handoff_sha256"] == shared.digest(handoff)
            and authority["inventory_sha256"] == shared.digest(value["inventory"]), "final_sensor_guard_actual_context_changed")
        for name in ("snapshot", "request_snapshot", "current_snapshot"):
            capture(bundle[name])
        _require(any(_same(bundle["request"], row["request"]) and _same(bundle["decision"], row["response"])
            for row in accepted if row["request"]["stage"] == "final"), "final_sensor_guard_without_accepted_judgment")
        if session["response"] is not None:
            _require(_same(session["response"]["receipt"], guard.check_bundle(bundle, authority)),
                "final_sensor_guard_receipt_not_independently_recomputed")
            if session["status"] == "received":
                guard_receipts.append(session["response"]["receipt"])
    application, witness = value["application"], value["application_witness"]
    switches = value["rollout"]["control_capability_audit"]["switches"]
    events = [row for row in value["rollout"]["events"] if row["event"] == "final_sensor_once_only_application"]
    if application is None:
        _require(witness is None and not switches and not events and value["rollout"]["switch"] is None,
            "final_sensor_model_changed_without_once_only_application")
    else:
        receipts = [row for row in guard_receipts if row["receipt_id"] == application["consumed_receipt_id"] and row["passed"]]
        _require(len(receipts) == 1 and type(witness) is dict and set(witness) == {"state", "controller", "actual_vehicle", "inventory_sha256"}
            and witness["inventory_sha256"] == shared.digest(value["inventory"])
            and _same(witness["actual_vehicle"], value["actual_vehicle"])
            and application["state_sha256"] == shared.digest(witness["state"])
            and application["controller_sha256"] == shared.digest(witness["controller"])
            and witness["state"]["time_s"] == application["time_s"] == complete["application_time_s"]
            and shared.digest(witness["state"]) in traced
            and application["time_s"] in plant_times and application["factor"] == receipts[0]["factor_estimate"]
            and application["action"] == receipts[0]["selected_action"] and application["snapshot_id"] == receipts[0]["snapshot_id"]
            and application["mode"] == inputs["mode"] and len(events) == 1
            and all(events[0].get(key) == val for key, val in application.items()), "final_sensor_application_witness_or_receipt_reassigned")
        changed = application["action"] == execution.ADOPT
        _require(application["control_model_changed"] is changed
            and (value["rollout"]["switch"] == {"factor": application["factor"], "time_s": application["time_s"]}
                and len(switches) == 1 if changed else value["rollout"]["switch"] is None and not switches),
            "final_sensor_application_not_actual_control_switch")
    for fallback in value["fallback_ledger"]:
        _require(fallback["action"] == execution.CONTINUE and fallback["basis"] == "source_registered_retain_nominal"
            and all(fallback[key] is False for key in ("model_judgment", "model_replacement_claimed", "control_model_changed")),
            "final_sensor_fallback_mislabelled_ai_or_model_replacement")
    return {"saved_continuation_consistent": True, "same_actual_initial_state_and_actuators": True,
        "physical": physical, "native_judgments": native, "fixture_judgments": fixtures,
        "explicit_delegations": delegated, "tracking_requested_by_accepted_judgment": bool(tracks),
        "accepted_actions": [row["accepted_action"] for row in accepted], "application": application,
        "fallback_count": len(value["fallback_ledger"]), "declined_work": value["declined_work"],
        "independent_guard_receipts_recomputed": len(guard_receipts),
        "guard_process_authentication_verified": False, "physical_reexecution": False,
        "continuous_uncertainty_envelope_verified": False, "free_prose_verified": False}


def verify_prefix_supervision(value, *, inputs, prefix, rpc):
    from . import starship_final_prefix_supervision as supervision
    binding = {"run_id": inputs["context"]["run_id"], "plan_sha256": inputs["plan_sha256"],
        "source_sha256": shared.digest(inputs["source_sha256"]), "mode": inputs["mode"],
        "policy_sha256": shared.digest(inputs["supervision_policy"])}
    _require(type(value) is dict and value.get("schema") == supervision.SCHEMA
        and all(value.get(key) == val for key, val in binding.items())
        and _same(value.get("policy"), inputs["supervision_policy"])
        and value.get("scope_sha256") == shared.digest({"context": inputs["context"], "phase": "prefix", "plan": inputs["plan_sha256"]})
        and value.get("provider_calls_by_worker") == 0 and all(value.get(name) is False for name in (
            "booster_initial_burn_selected_by_ai", "numerical_gnc_waited_for_model", "physical_clock_paused",
            "state_or_inventory_assigned", "execution_permission_granted")), "final_prefix_supervision_binding_or_claim")
    witnesses = prefix.get("final_supervision_actual_witnesses", []) + prefix.get("booster_run", {}).get("final_supervision_actual_witnesses", [])
    indexed, requests = {}, value["requests"]
    for witness in witnesses:
        _require(type(witness) is dict and witness.get("schema") == "missionos.starship_final_actual_supervision_witness.v1"
            and witness.get("origin") == "actual_plant_tick_before_supervision"
            and witness.get("state_or_inventory_assigned") is False
            and witness["state_sha256"] == shared.digest(witness["state"])
            and witness["vehicle_sha256"] == shared.digest(witness["vehicle"])
            and witness["time_s"] == witness["state"]["time_s"] and type(witness["request_ids"]) is list,
            "final_prefix_actual_seam_witness_changed")
        for request_id in witness["request_ids"]:
            _require(request_id not in indexed, "final_prefix_actual_witness_replayed")
            indexed[request_id] = witness
    ids, judgments, native, fixture, safety = set(), {}, {}, {}, 0
    for judgment in value["judgments"]:
        _require(judgment["request_id"] not in judgments, "final_prefix_supervision_judgment_replayed")
        judgments[judgment["request_id"]] = judgment
    for row in requests:
        request_id, observation = row["request_id"], row["observation"]
        _require(request_id not in ids and request_id in indexed, "final_prefix_supervision_without_actual_tick")
        ids.add(request_id)
        witness = indexed[request_id]
        _require(witness["body"] == row["domain"] == observation["body"]
            and witness["phase"] == observation["phase"] and _same(witness["state"], observation["state"])
            and _same(witness["vehicle"], observation["vehicle"])
            and witness["state_sha256"] == observation["state_sha256"]
            and witness["vehicle_sha256"] == observation["vehicle_sha256"], "final_prefix_observer_not_actual_seam")
        exchange, judgment = rpc.get(request_id), judgments.get(request_id)
        if exchange is not None:
            frame = exchange["host"]["frame"]
            _require(_same(frame["request"], row["request"]) and frame["request_sha256"] == row["request_sha256"]
                and frame["started_wall_s"] == row["started_wall_s"]
                and frame["wall_deadline_s"] == row["wall_deadline_s"], "final_prefix_supervision_rpc_reassigned")
        if judgment is None:
            _require(row.get("expired_before_send") is True or value["pending_epoch"] is not None
                or any(item.get("request_id") == request_id for item in value["queued"]),
                "final_prefix_unaccounted_judgment")
            continue
        if judgment["accepted"]:
            _require(exchange is not None and exchange["client"] is not None and not exchange["client"]["expired"]
                and _same(judgment["result"], exchange["client"]["delivered_result"]),
                "final_prefix_judgment_without_delivered_host_result")
            result = supervision.validate_saved_judgment(row, judgment["result"], mode=inputs["mode"])
            _require(judgment["action"] == result["action"]
                and row["started_s"] <= judgment["time_s"] < row["deadline_s"]
                and judgment["native_model_judgment"] is (inputs["mode"] == "live")
                and judgment["physical_execution_authorized"] is False, "final_prefix_accepted_judgment_reassigned")
            counts = native if inputs["mode"] == "live" else fixture
            counts[row["domain"]] = counts.get(row["domain"], 0)+1
        else:
            _require(judgment.get("native_model_judgment") is False and judgment.get("safety_path") is not None,
                "final_prefix_safety_path_mislabelled_native")
            safety += 1
    _require(set(judgments) <= ids and set(indexed) == ids, "final_prefix_unaccounted_actual_seam_or_judgment")
    return {"actual_tick_projection_consistent": True, "request_count": len(ids), "native_by_domain": native,
        "fixture_by_domain": fixture, "safety_path_count": safety,
        "engineering_telemetry_is_diagnosis": False, "booster_ignition_selected_by_model": False,
        "source_authentication_verified": False}


def _verify_visual_epoch(episode, envelope, exchange, policy):
    """Join a later snapshot to the one original hold, never renew its budget."""
    _require(type(episode) is dict and set(episode) == {"episode_id", "event_id", "release_attempt_id",
        "started_s", "deadline_s", "started_wall_s", "wall_deadline_s"}
        and all(_number(episode[name]) and episode[name] >= 0 for name in (
            "started_s", "deadline_s", "started_wall_s", "wall_deadline_s"))
        and 0 < episode["deadline_s"]-episode["started_s"] <= policy["hold_s"]
        and math.isclose(episode["wall_deadline_s"]-episode["started_wall_s"],
            episode["deadline_s"]-episode["started_s"], rel_tol=0., abs_tol=1e-8),
        "final_visual_saved_original_episode")
    if envelope is None:
        return
    request = envelope["request"]
    _require(all(request[name] == episode[name] for name in ("episode_id", "event_id", "release_attempt_id", "deadline_s"))
        and episode["started_s"] <= request["snapshot_time_s"] < episode["deadline_s"]
        and envelope["started_wall_s"] == episode["started_wall_s"]
        and envelope["wall_deadline_s"] == episode["wall_deadline_s"], "final_visual_saved_envelope_renewed_epoch")
    if exchange is not None:
        frame = exchange["host"]["frame"]
        rpc_request = frame["request"]
        _require(rpc_request["domain"] == "visual" and rpc_request["request_id"] == request["request_id"]
            and rpc_request["time_s"] == episode["started_s"] and rpc_request["deadline_s"] == episode["deadline_s"]
            and frame["started_wall_s"] == episode["started_wall_s"]
            and frame["wall_deadline_s"] == episode["wall_deadline_s"]
            and _same(rpc_request["observation"], envelope), "final_visual_saved_rpc_renewed_epoch")


def _counter_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _visual_observer_ticks(prefix, ship):
    """Keep the post-observer closing tick; omit only the known early break.

    A zero-duration last tick is not enough to identify an unobserved tick.
    The ordinary count13 decision closes inventory AFTER visual observation.
    Only the source-bound before-observer closed/unclosed branches skip
    that final observation. No earlier or positive-duration tick is removed.
    """
    orbital = [(position, index, row) for position, (index, row) in enumerate(ship)
        if row["phase"] == "orbital_coast"]
    if not orbital:
        return orbital, None
    position, index, last = orbital[-1]
    _require(position == len(ship)-1, "final_visual_counter_orbital_prefix_terminal_tick_required")
    termination = prefix["outcome"]["termination"]
    terminal = [event for event in prefix["events"] if event["event"] == termination]
    _require(termination in ("restored_deployment_handoff", "restored_deployment_unclosed")
        and len(terminal) == 1 and terminal[0]["time_s"] == last["time_s"]
        and last["next_time_s"] == last["time_s"],
        "final_visual_counter_source_bound_terminal_branch_required")
    event = terminal[0]
    if termination == "restored_deployment_unclosed":
        _require(event.get("detail") == "no closed inventory before nominal return; prefix cannot dispatch return",
            "final_visual_counter_unclosed_terminal_branch_changed")
        return orbital[:-1], index
    _require(event.get("state_reset") is False, "final_visual_counter_terminal_state_reset")
    if event.get("detail") == "actual inventory already closed; same actual coast state retained":
        return orbital[:-1], index
    _require(event.get("detail") == "actual inventory closed; same state retained before nominal return",
        "final_visual_counter_unknown_handoff_terminal_branch")
    return orbital, None


def _verify_visual_counter_timeline(value, *, prefix, inventory, inputs):
    """Bind observed counts to exact actual pre-actuation Ship ticks.

    Counter attempt source_time is the first observer tick that SEES the new
    count. It is not the earlier release action time. No tolerance, timestamp,
    counter value, physical receipt, state or original file is changed here.
    """
    from .starship_anomaly_events import ConfirmationChannel
    run_id = inputs["context"]["run_id"]
    policy = inputs["visual_policy"]
    clock = prefix["concurrent_execution"]
    _require(clock.get("schema") == "missionos.ship_booster_clock.v1"
        and clock.get("observers_run_before_each_body_step") is True
        and clock.get("posthoc_ordering") is False, "final_visual_counter_actual_executor_clock_required")
    ship = [(index, row) for index, row in enumerate(clock["tick_order"]) if row["body_id"] == "ship"]
    _require(ship, "final_visual_counter_actual_ship_ticks_missing")
    for position, (index, row) in enumerate(ship):
        _require(type(row["sequence"]) is int and row["sequence"] == index
            and _counter_time(row["time_s"]) and _counter_time(row["next_time_s"])
            and row["time_s"] <= row["next_time_s"]
            and type(row["released_count"]) is int and 0 <= row["released_count"] <= 26
            and (position == 0 or ship[position-1][1]["next_time_s"] == row["time_s"]),
            "final_visual_counter_actual_tick_chain_changed")
    observed, excluded_terminal = _visual_observer_ticks(prefix, ship)
    by_time = {}
    for position, index, row in observed:
        by_time.setdefault(row["time_s"], []).append((position, index, row))
    receipts = inventory["release_receipts"]
    satellites = prefix["satellites"]
    _require(type(receipts) is list and len(receipts) == len(satellites) == inventory["released_count"]
        and inventory["retained_count"]+inventory["released_count"] == 26
        and all(satellite["id"] == f"satellite_{index+1:02d}"
            and _wire(satellite["receipt"]) == _wire(receipt)
            and _counter_time(receipt["time_s"])
            and receipt["parent_before_state"]["time_s"] == receipt["parent_after_state"]["time_s"] == receipt["time_s"]
            and (index == 0 or receipts[index-1]["time_s"] < receipt["time_s"])
            for index, (satellite, receipt) in enumerate(zip(satellites, receipts, strict=True))),
        "final_visual_counter_inventory_not_actual_rigid_release_receipts")
    # Yield/observer comes before release on a tick: an action at this exact
    # clock is still absent from that tick's incoming observed count.
    for _, row in ship:
        _require(row["released_count"] == sum(receipt["time_s"] < row["time_s"] for receipt in receipts),
            "final_visual_counter_actual_tick_count_not_pre_actuation")

    def observed_tick(when):
        rows = by_time.get(when, [])
        _require(len(rows) == 1, "final_visual_counter_source_not_unique_actual_observation_tick")
        return rows[0]

    attempts = value["counter_attempts"]
    _require(type(attempts) is list, "final_visual_counter_actual_attempts_required")
    # Replay only this numerical observation channel, never plant dynamics or
    # image/AI logic. Exact full arrays bind cadence, delay, barrier and every
    # observed post-trigger increment, including attempts after visual resume.
    replay = ConfirmationChannel(run_id, first_delay_s=policy["first_counter_delay_s"])
    physical_count = 0
    for _, _, row in observed:
        count, now = row["released_count"], row["time_s"]
        _require(count in (physical_count, physical_count+1), "final_visual_counter_actual_observer_inventory_gap")
        if count != physical_count:
            if count == policy["trigger_released_count"] or replay.current_attempt is not None:
                _require(replay.count == count-1, "final_visual_counter_actual_counter_unprimed")
                replay.attempt(now, count)
            physical_count = count
        replay.observe(now, count)
    _require(_wire(attempts) == _wire(replay.attempts), "final_visual_counter_complete_actual_attempt_replay_changed")
    _require(_wire(value["counter_receipts"]) == _wire(replay.receipts),
        "final_visual_counter_complete_arrival_cadence_barrier_replay_changed")
    acknowledged = [event for event in value["events"] if event["event"] == "release_ack_observed"]
    _require(_wire(acknowledged) == _wire([{"event": "release_ack_observed", **attempt,
        "separation_inferred_from_ack": False} for attempt in replay.attempts]),
        "final_visual_counter_complete_ack_event_replay_changed")
    channel = ConfirmationChannel(run_id, first_delay_s=policy["first_counter_delay_s"])
    channel.attempts = shared.parse(_wire(attempts))
    joins = []
    for index, attempt in enumerate(attempts, 1):
        expected = attempt["expected_count"]
        _require(type(expected) is int and policy["trigger_released_count"] <= expected <= inventory["released_count"]
            and expected == policy["trigger_released_count"]+index-1
            and attempt["release_attempt_id"] == "a-"+shared.digest([run_id, index])[:20]
            and attempt["acknowledged"] is True and _counter_time(attempt["source_time_s"]),
            "final_visual_counter_attempt_identity_or_expected_count_changed")
        position, actual_index, current = observed_tick(attempt["source_time_s"])
        _require(position > 0, "final_visual_counter_attempt_missing_previous_actual_tick")
        previous_index, previous = ship[position-1]
        release = receipts[expected-1]
        first_count_position = next((pos for pos, (_, row) in enumerate(ship) if row["released_count"] == expected), None)
        between = [receipt for receipt in receipts if previous["time_s"] <= receipt["time_s"] < current["time_s"]]
        _require(position == first_count_position and current["released_count"] == expected
            and previous["phase"] == "orbital_coast" and previous["released_count"] == expected-1
            and previous["next_time_s"] == current["time_s"]
            and release["time_s"] == previous["time_s"] < current["time_s"]
            and len(between) == 1 and _wire(between[0]) == _wire(release),
            "final_visual_counter_attempt_not_first_post_release_actual_tick")
        acknowledgements = [row for row in value["events"] if row["event"] == "release_ack_observed"
            and row.get("release_attempt_id") == attempt["release_attempt_id"]]
        _require(len(acknowledgements) == 1 and all(acknowledgements[0].get(key) == val for key, val in attempt.items())
            and acknowledgements[0].get("separation_inferred_from_ack") is False,
            "final_visual_counter_observed_ack_event_changed")
        joins.append({"attempt_index": index-1, "attempt_sha256": shared.digest(attempt),
            "previous_tick_pointer": f"/prefix/concurrent_execution/tick_order/{previous_index}",
            "previous_tick_sha256": shared.digest(previous), "observed_tick_pointer": f"/prefix/concurrent_execution/tick_order/{actual_index}",
            "observed_tick_sha256": shared.digest(current), "release_receipt_index": expected-1,
            "release_receipt_sha256": shared.digest(release), "release_action_time_s": release["time_s"],
            "ack_observed_time_s": current["time_s"]})
    for receipt in value["counter_receipts"]:
        _require(channel.accept(receipt, receipt["available_time_s"]), "final_visual_counter_duplicate_or_reordered")
        _, _, source_tick = observed_tick(receipt["source_time_s"])
        _require(receipt["confirmed_count"] == source_tick["released_count"],
            "final_visual_counter_sample_differs_from_actual_pre_actuation_tick")
    return {"schema": "missionos.starship_final_visual_counter_check.v1", "attempts": joins,
        "counter_receipts_checked": len(value["counter_receipts"]), "actual_tick_clock_checked": True,
        "arrival_and_sequence_checked_by_original_channel": True,
        "complete_counter_channel_replay_exact": True, "actual_observer_tick_count": len(observed),
        "excluded_pre_observer_terminal_tick_pointer": None if excluded_terminal is None
            else f"/prefix/concurrent_execution/tick_order/{excluded_terminal}",
        "timestamps_or_thresholds_changed": False, "physical_reexecution": False,
        "model_calls": 0, "source_authentication_established": False}


def verify_visual(value, *, inputs, host, rpc, artifacts, inventory, prefix):
    from . import starship_final_visual as visual
    from .starship_visual_contract import validate_request
    binding = {"run_id": inputs["context"]["run_id"], "plan_sha256": inputs["plan_sha256"],
        "source_sha256": shared.digest(inputs["source_sha256"]), "mode": inputs["mode"],
        "policy_sha256": shared.digest(inputs["visual_policy"])}
    _require(type(value) is dict and value.get("schema") == visual.SCHEMA
        and all(value.get(key) == val for key, val in binding.items())
        and _same(value.get("policy"), inputs["visual_policy"])
        and all(value.get(name) is False for name in ("inventory_assigned", "plant_state_assigned", "automatic_retry"))
        and type(host) is dict and host.get("schema") == "missionos.starship_final_visual_host.v1"
        and all(host.get(key) == val for key, val in binding.items())
        and host.get("automatic_retry") is host.get("inventory_or_execution_authority_granted") is False,
        "final_visual_saved_scope_or_claims_changed")
    _require(type(value["requests"]) is list and len(value["requests"]) <= inputs["visual_policy"]["maximum_judgments"]
        and len(value["observation_requests"]) <= inputs["visual_policy"]["maximum_observations"],
        "final_visual_registered_request_budget")
    episode = value["episode"]
    if episode is not None or value["requests"]:
        _verify_visual_epoch(episode, None, None, inputs["visual_policy"])
        holds = [row for row in value["events"] if row["event"] == "precautionary_hold"]
        _require(len(holds) == 1 and holds[0]["time_s"] == episode["started_s"],
            "final_visual_original_hold_event_changed")
    attempts = {row["envelope_sha256"]: row for row in host["attempts"]}
    _require(len(attempts) == len(host["attempts"]), "final_visual_host_attempt_repeated")
    requests = {}
    for envelope in value["requests"]:
        request = validate_request(envelope["request"])
        _require(all(envelope.get(key) == val for key, val in binding.items())
            and envelope["schema"] == visual.ENVELOPE and envelope["request_sha256"] == shared.digest(request)
            and envelope["envelope_sha256"] == shared.digest({k: v for k, v in envelope.items() if k != "envelope_sha256"})
            and request["request_id"] not in requests, "final_visual_envelope_or_request_reassigned")
        requests[request["request_id"]] = envelope
        exchange = rpc.get(request["request_id"])
        _verify_visual_epoch(episode, envelope, exchange, inputs["visual_policy"])
        frames = request["manifest"]["frames"]
        _require(envelope["frame_refs"] == [{"frame_id": f["frame_id"], "path": "frames/"+f["image_sha256"]+".png",
            "sha256": f["image_sha256"]} for f in frames], "final_visual_frame_refs_changed")
        for frame in frames:
            raw = artifacts.get(frame["image_sha256"])
            _require(type(raw) is bytes and sha256(raw).hexdigest() == frame["image_sha256"]
                and raw.startswith(b"\x89PNG\r\n\x1a\n"), "final_visual_raw_pixels_missing_or_changed")
    _require({name for name, exchange in rpc.items() if exchange["host"]["frame"]["request"]["domain"] == "visual"}
        <= set(requests), "final_visual_unrecorded_rpc_exchange")
    native, fixture, accepted_hashes = 0, 0, set()
    for row in value["results"]:
        envelopes = [e for e in requests.values() if e["envelope_sha256"] == row["envelope_sha256"]]
        _require(len(envelopes) == 1 and row["envelope_sha256"] not in accepted_hashes,
            "final_visual_result_without_unique_request")
        envelope = envelopes[0]
        if not row["accepted"]:
            _require(row["rejection"] is not None, "final_visual_rejection_unlabelled")
            continue
        accepted_hashes.add(row["envelope_sha256"])
        attempt, reply = attempts.get(row["envelope_sha256"]), row["reply"]
        exchange = rpc.get(envelope["request"]["request_id"])
        _require(attempt is not None and _same(attempt["response"], reply)
            and exchange is not None and exchange["client"] is not None and exchange["client"]["expired"] is False
            and _same(exchange["client"]["delivered_result"], reply)
            and all(reply.get(key) == val for key, val in binding.items()) and reply["schema"] == visual.HOST_RESULT
            and reply["envelope_sha256"] == envelope["envelope_sha256"] and reply["request_sha256"] == envelope["request_sha256"]
            and reply["result_sha256"] == shared.digest(reply["result"]), "final_visual_accepted_result_not_external_host")
        visual.validate_result(reply["result"], envelope["request"], inputs["mode"])
        _require(envelope["request"]["snapshot_time_s"] <= row["time_s"]
            and row["time_s"]+inputs["visual_policy"]["apply_margin_s"] < envelope["request"]["deadline_s"],
            "final_visual_accepted_after_original_deadline")
        native += inputs["mode"] == "live"
        fixture += inputs["mode"] == "fixture"
    _require(set(attempts) <= {row["envelope_sha256"] for row in requests.values()}
        and value["native_judgments"] == native
        and value["ai_visual_work_completed"] is bool(inputs["mode"] == "live" and native
            and value["closed_reason"] == "accepted_visual_proposal"), "final_visual_native_work_or_extra_attempt_changed")
    # Join observations to actual pre-actuation ticks and replay the finite
    # delayed counter, without rerunning physics or trusting image diagnoses.
    counter = _verify_visual_counter_timeline(value, prefix=prefix, inventory=inventory, inputs=inputs)
    for row in value["results"]:
        if row["accepted"] and row["reply"]["result"]["action"] == "continue":
            envelope = next(e for e in requests.values() if e["envelope_sha256"] == row["envelope_sha256"])
            request = envelope["request"]
            fresh = [r for r in value["counter_receipts"] if r["available_time_s"] <= row["time_s"]
                and row["time_s"]-r["source_time_s"] <= inputs["visual_policy"]["counter_max_age_s"]+1e-9
                and r["release_attempt_id"] == request["release_attempt_id"]
                and r["confirmed_count"] == request["telemetry"]["expected_count"]]
            _require(fresh and request["telemetry"]["orbit_release_feasible"] is True,
                "final_visual_continue_without_fresh_independent_count")
    return {"native_judgments": native, "fixture_judgments": fixture,
        "observation_requests": len(value["observation_requests"]), "counter_source_count_checked": True,
        "counter_timeline": counter,
        "image_hashes_checked": True, "image_semantic_truth_established": False,
        "free_prose_verified": False, "inventory_modified_by_ai": False}


def verify_numerical_tasks(tasks, sensor, *, inputs, artifacts, artifact_refs):
    """Cross-check independently retained child files and outer PID census.

    Parent-observed PID relationships plus byte pins are consistency evidence,
    not OS attestation. The numerical result is separately recomputed above.
    """
    import gzip
    _require(type(artifact_refs) is dict and all(type(name) is str and _hash(identity)
        and name == Path(name).as_posix() and not Path(name).is_absolute() and ".." not in Path(name).parts
        and identity in artifacts for name, identity in artifact_refs.items()), "final_external_safe_artifact_refs_required")
    def raw_named(name):
        _require(name in artifact_refs, "final_outer_task_artifact_missing")
        raw = artifacts[artifact_refs[name]]
        _require(type(raw) is bytes and sha256(raw).hexdigest() == artifact_refs[name], "final_outer_task_artifact_hash")
        return raw
    outer = shared.parse(raw_named("outer-process.json"))
    base_fields = {"schema", "plan_sha256", "source_sha256", "worker_pid", "worker_exit_code",
        "observed_children", "observed_child_groups_absent", "all_children_exhaustively_observed", "os_attestation"}
    version_two = type(outer) is dict and outer.get("schema") == "missionos.starship_final_outer_process.v2"
    _require(type(outer) is dict and set(outer) == (base_fields | {"timing_sha256"} if version_two else base_fields)
        and outer["schema"] in ("missionos.starship_final_outer_process.v1", "missionos.starship_final_outer_process.v2")
        and outer["plan_sha256"] == inputs["plan_sha256"]
        and outer["source_sha256"] == shared.digest(inputs["source_sha256"])
        and type(outer["worker_pid"]) is int and outer["worker_pid"] > 0 and outer["worker_exit_code"] == 0
        and outer["observed_child_groups_absent"] is True
        and outer["all_children_exhaustively_observed"] is outer["os_attestation"] is False,
        "final_outer_process_pin_or_claims_changed")
    _require(version_two or "src/runtime/starship_final_timing.py" not in inputs["source_sha256"],
        "final_current_source_requires_suspend_inclusive_timing")
    mission_timing = None
    if version_two:
        from .starship_final_timing import verify_completed
        mission_timing = {}
        _require(outer["timing_sha256"] == artifact_refs.get("outer-timing.json"), "final_outer_timing_pin_changed")
        for filename, component in (("outer-timing.json", "outer"),
                ("worker-timing.json", "native_worker_before_serialization")):
            stamp = shared.parse(raw_named(filename))
            _require(type(stamp) is dict and set(stamp) == {"component", "plan_sha256", "source_sha256", "timing"}
                and stamp["component"] == component and stamp["plan_sha256"] == inputs["plan_sha256"]
                and stamp["source_sha256"] == shared.digest(inputs["source_sha256"]), "final_mission_timing_binding_changed")
            mission_timing[component] = verify_completed(stamp["timing"], maximum_s=inputs["bounds"]["per_mission_wall_s"])
        outer_stamp = shared.parse(raw_named("outer-timing.json"))["timing"]
        worker_stamp = shared.parse(raw_named("worker-timing.json"))["timing"]
        _require(all(outer_stamp["start"][key] <= worker_stamp["start"][key]
            <= worker_stamp["end"][key] <= outer_stamp["end"][key] for key in ("monotonic_s", "utc_s")),
            "final_worker_timing_not_contained_by_outer")
    children = {}
    for row in outer["observed_children"]:
        _require(type(row) is dict and row["ppid"] == outer["worker_pid"] and row["pid"] == row["pgid"]
            and row["pid"] > 0 and row["pid"] not in children
            and _number(row["first_seen_wall_s"]) and row["first_seen_wall_s"] <= row["last_seen_wall_s"],
            "final_outer_child_parent_or_group_changed")
        children[row["pid"]] = row
    _require(type(tasks) is list and len(tasks) <= 3, "final_numerical_task_budget")
    sensor = sensor or {"forecast_batches": [], "guard_sessions": []}
    expected = [("forecast", {"jobs": batch["jobs"]}, batch) for batch in sensor["forecast_batches"]]
    expected += [("guard", {"bundle": expanded_guard_bundle(row, sensor)
        if "forecasts_sha256" in row["bundle"] else row["bundle"], "authority": row["authority"]}, row)
        for row in sensor["guard_sessions"]]
    _require(len(tasks) == len(expected), "final_numerical_unaccounted_task_count")
    seen_pids, used_sessions, guard_processes, numerical_results = set(), set(), 0, 0
    for task_index, task in enumerate(tasks):
        _require(task.get("schema") == "missionos.starship_final_numerical_task_owner.v1"
            and task.get("kind") in ("forecast", "guard") and task.get("automatic_retry") is False
            and task.get("credential_free") is True and type(task.get("cancelled")) is bool
            and task.get("maximum_concurrent_children") == (2 if task["kind"] == "forecast" else 1),
            "final_numerical_owner_schema_or_retry")
        kind = task["kind"]
        matches = [(i, payload, session) for i, (name, payload, session) in enumerate(expected)
            if name == kind and i not in used_sessions]
        _require(matches, "final_numerical_owner_unaccounted_session")
        expected_index, payload, session = matches[0]
        used_sessions.add(expected_index)
        jobs = payload["jobs"] if kind == "forecast" else [payload]
        started = [row for row in task["events"] if row["event"] == "started"]
        reaped = [row for row in task["events"] if row["event"] == "reaped"]
        full_results = {}
        _require(len({row["index"] for row in started}) == len(started)
            and len({row["index"] for row in reaped}) == len(reaped), "final_numerical_child_retry_or_duplicate_reap")
        for row in started:
            index, pid = row["index"], row["pid"]
            _require(type(index) is int and 0 <= index < len(jobs) and type(pid) is int and pid > 0
                and pid not in seen_pids and pid != outer["worker_pid"], "final_numerical_child_pid_or_index")
            seen_pids.add(pid)
            base = f"numerical-{task_index}-{kind}/{index}/"
            job_raw = raw_named(base+"job.json")
            job = shared.parse(job_raw)
            _require(_same(job, {"schema": "missionos.starship_final_numerical_job.v1", "kind": kind, "payload": jobs[index]}),
                "final_numerical_job_not_selected_current_capture")
            # Logs are retained even when empty; no content is interpreted as AI.
            raw_named(base+"worker.log")
            exits = [event for event in reaped if event["index"] == index and event["pid"] == pid]
            if not exits:
                _require(task["cancelled"] and session["status"] not in ("accepted", "received"),
                    "final_numerical_accepted_without_child_exit")
                continue
            _require(len(exits) == 1 and exits[0]["time"] >= row["time"], "final_numerical_reap_epoch")
            if exits[0]["exit_code"] != 0:
                _require(task["cancelled"] and session["status"] not in ("accepted", "received"),
                    "final_numerical_failed_child_result_adopted")
                continue
            ready = shared.parse(raw_named(base+"process-ready.json"))
            ack = shared.parse(raw_named(base+"parent-observed.json"))
            _require(type(ready) is dict and set(ready) == {"schema", "job_sha256", "pid", "ppid", "pgid"}
                and ready["schema"] == "missionos.starship_final_numerical_process_ready.v1"
                and ready["job_sha256"] == sha256(job_raw).hexdigest()
                and ready["pid"] == ready["pgid"] == pid and ready["ppid"] == outer["worker_pid"]
                and type(ack) is dict and set(ack) == {"schema", "job_sha256", "pid", "ppid", "pgid", "plan_sha256", "outer_host_pid", "observed_wall_s"}
                and ack["schema"] == "missionos.starship_final_numerical_parent_observation.v1"
                and all(ack[key] == ready[key] for key in ("job_sha256", "pid", "ppid", "pgid"))
                and ack["plan_sha256"] == inputs["plan_sha256"]
                and type(ack["outer_host_pid"]) is int and ack["outer_host_pid"] > 1
                and ack["outer_host_pid"] not in (pid, outer["worker_pid"])
                and _number(ack["observed_wall_s"]) and row["time"] <= ack["observed_wall_s"] <= exits[0]["time"]
                and pid in children and children[pid]["first_seen_wall_s"] <= ack["observed_wall_s"],
                "final_numerical_ready_barrier_not_outer_observed_process")
            compressed = raw_named(base+"result.json.gz")
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(MAXIMUM_RAW_BYTES+1)
            _require(len(raw) <= MAXIMUM_RAW_BYTES, "final_numerical_result_expansion_budget")
            result = shared.parse(raw)
            _require(type(result) is dict and set(result) == {"schema", "kind", "job_sha256", "checker_pid", "credential_free", "result"}
                and result["schema"] == "missionos.starship_final_numerical_result.v1" and result["kind"] == kind
                and result["job_sha256"] == sha256(job_raw).hexdigest() and result["checker_pid"] == pid
                and result["credential_free"] is True, "final_numerical_raw_result_binding")
            numerical_results += 1
            full_results[index] = result["result"]
            if kind == "guard" and session["response"] is not None:
                callback = session["response"]["receipt"]
                _require(_same(result["result"], callback), "final_numerical_callback_not_retained_raw_result")
                if kind == "guard":
                    process = session["response"]["process_evidence"]
                    _require(process.get("checker_pid") == pid and process.get("job_sha256") == sha256(job_raw).hexdigest()
                        and process.get("exit_code") == 0 and process.get("credential_free") is True
                        and process.get("fixture_only") is False and pid in children,
                        "final_guard_process_not_independently_observed_child")
                    guard_processes += 1
        if kind == "forecast" and session.get("response_sha256") is not None:
            _require(set(full_results) == set(range(6))
                and shared.digest([full_results[index] for index in range(6)]) == session["response_sha256"],
                "final_forecast_response_digest_not_retained_children")
    return {"mission_timing": mission_timing, "owned_task_count": len(tasks), "raw_numerical_results_checked": numerical_results,
        "guard_child_parent_group_consistent": guard_processes, "observed_child_groups_absent": True,
        "complete_child_census_claimed": False, "os_attestation": False,
        "process_authentication_established": False, "physical_reexecution": False}


def verify_final_mission(record_raw, *, expected_record_sha256, expected_inputs, artifacts,
                         host_records, expected_host_sha256, artifact_refs):
    """Verify retained bytes only; return consistency, outcome and work separately.

    Every expected pin must come from the externally reviewed plan/outer
    manifest. A producer's self-declared success is never an input authority.
    """
    from .starship_sixdof_verifier import _Invalid as SavedInvalid
    result = {"schema": VERIFICATION_SCHEMA, "passed": False, "issues": [], "checks": {},
        "record_sha256": expected_record_sha256, "physical_execution": False,
        "qualification_refreshed": False, "runtime_admission": False,
        "source_authentication_established": False, "host_signature_verified": False,
        "human_identity_authenticated": False, "continuous_uncertainty_envelope_verified": False,
        "independent_physical_model": False, "real_vehicle_validated": False,
        "free_prose_verified": False, "model_calls": 0, "plant_steps": 0}
    try:
        inputs = validate_verification_inputs(expected_inputs)
        result.update(context=inputs["context"], plan_sha256=inputs["plan_sha256"],
            source_sha256=shared.digest(inputs["source_sha256"]), mode=inputs["mode"], case_id=inputs["case_id"])
        value = _raw(record_raw, expected_record_sha256)
        _require(type(value) is dict and set(value) == RECORD_KEYS and value["schema"] == SCHEMA
            and _same(value["execution"], execution_binding(inputs)) and value["claim_limits"] == CLAIM_LIMITS,
            "final_aggregate_schema_binding_or_claims_changed")
        _require(type(value["supplemental"]) is dict and set(value["supplemental"]) == {
            "supervision", "visual", "client", "numerical_tasks"}, "final_supplemental_components_required")
        result["checks"]["current_binding"] = verify_current_binding(inputs)
        # Forecast trajectories already have independently retained numerical
        # child files. Reconstruct their exact preimages from externally pinned
        # bytes; all numerical/PID/job/guard checks below still run unchanged.
        from .starship_final_record_storage import decode_record
        value = decode_record(value, artifacts=artifacts, artifact_refs=artifact_refs)
        hosts = verify_host_wrappers(host_records, expected_host_sha256, inputs)
        exchanges = verify_rpc(value["supplemental"]["client"], hosts["supplemental"], inputs)
        result["checks"]["pre_dispatch"] = pre = verify_pre_dispatch(value, inputs, artifacts, hosts["restored"],
            visual_host=hosts["visual"], rpc=exchanges)
        result["checks"]["prefix_supervision"] = verify_prefix_supervision(value["supplemental"]["supervision"],
            inputs=inputs, prefix=value["prefix"], rpc=exchanges)
        if pre["closed_inventory"]:
            result["checks"]["visual"] = pre.pop("visual")
        if pre["closed_inventory"] and not pre.get("governed_no_handoff", False):
            result["checks"]["sensor"] = sensor = verify_sensor_suffix(value["sensor_return"], inputs=inputs,
                handoff=value["handoff"], harness=value["fault_harness"], host=hosts["sensor"])
            # Every sensor proposal that reached a host must also be carried by
            # the exact legitimate sensor RPC domain, never a relabelled phase.
            for session in value["sensor_return"]["decision_sessions"]:
                request = session["request"]
                exchange = exchanges.get(request["request_id"])
                if session["response"] is not None:
                    _require(exchange is not None and exchange["host"]["frame"]["request"]["domain"] == "sensor"
                        and _same(exchange["host"]["frame"]["request"]["observation"], request)
                        and exchange["client"] is not None
                        and (session["status"] != "accepted" or exchange["client"]["expired"] is False
                            and _same(exchange["client"]["delivered_result"], session["response"])),
                        "final_sensor_not_delivered_by_bound_rpc")
            result["outcome"] = {"ship_contact_limits_met": sensor["physical"]["contact_limits_met"],
                "booster_contact_envelope_met": pre["booster_contact_envelope_met"],
                "all_required_physical_gates_met": bool(sensor["physical"]["contact_limits_met"] and pre["booster_contact_envelope_met"]),
                "contact": sensor["physical"]["contact"], "retained_count": pre["retained_count"],
                "operator_principal": "preauthorized_scripted_http_simulation_principal",
                "real_human_review": False, "normal_scope": "no_additional_return_fault_with_O0_ascent_outages"}
            prefix_work, visual_work = result["checks"]["prefix_supervision"], result["checks"]["visual"]
            deep = sum(attempt.get("provider") == "deepseek" and type(attempt.get("judgment")) is dict
                and attempt["judgment"].get("status") == "valid"
                and attempt["judgment"].get("model_inference_invoked") is True
                for session in hosts["restored"]["decision_host"]["requests"] for attempt in session["attempts"])
            native_roles = {"ascent_supervision": prefix_work["native_by_domain"].get("ascent", 0) > 0,
                "booster_continuation_review": prefix_work["native_by_domain"].get("booster", 0) > 0,
                "deployment": pre["deployment_native"], "return_selection": pre["return_native"],
                "visual_observation": visual_work["native_judgments"] > 0,
                "return_sensor_judgment": sensor["native_judgments"] > 0,
                "deep_comparison": deep > 0 or sensor["explicit_delegations"] > 0}
            result["work"] = {"native_role_work_observed": native_roles,
                "all_native_role_work_observed": inputs["mode"] == "live" and all(native_roles.values()),
                "scripted_scope_amendment_consistent": pre["operator_amendment_consistency_verified"],
                "sensor_tracking_requested": sensor["tracking_requested_by_accepted_judgment"],
                "sensor_once_only_application": sensor["application"] is not None,
                "sensor_fallback_count": sensor["fallback_count"],
                "native_booster_review_is_ignition_permission": False,
                "normal_case_is_fault_free_ascent": False,
                "native_role_observed_is_judgment_quality_score": False}
            accounted = {row["request_id"] for row in value["supplemental"]["supervision"]["requests"]}
            accounted |= {row["request"]["request_id"] for row in value["supplemental"]["visual"]["requests"]}
            accounted |= {row["request"]["request_id"] for row in value["sensor_return"]["decision_sessions"]}
            _require(set(exchanges) <= accounted, "final_unaccounted_supplemental_provider_request")
        else:
            _require(not hosts["sensor"].get("sessions"), "final_failed_prefix_borrowed_sensor_work")
            if pre.get("governed_no_handoff", False):
                _require(value["handoff"] is value["fault_harness"] is value["sensor_return"] is None,
                    "final_governed_refusal_started_another_return")
                result["outcome"] = {"all_required_physical_gates_met": False, "ship_contact_limits_met": False,
                    "booster_contact_envelope_met": pre["booster_contact_envelope_met"], "contact": None,
                    "termination": "return_inhibited_by_governance", "governed_no_handoff": True,
                    "refusal_reason": pre["refusal"]["refusal_reason"], "completed_v1_flight": False,
                    "retained_count": pre["retained_count"]}
            else:
                result["outcome"] = {"all_required_physical_gates_met": False, "termination": pre["termination"]}
        result["checks"]["numerical_processes"] = verify_numerical_tasks(value["supplemental"]["numerical_tasks"],
            value["sensor_return"], inputs=inputs, artifacts=artifacts, artifact_refs=artifact_refs)
        result["passed"] = True  # Saved consistency, independently of success.
        result["consistency_is_flight_success"] = False
    except SavedInvalid as error:
        result["issues"].append(error.issue)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, OverflowError) as error:
        result["issues"].append({"type": type(error).__name__, "reason": str(error)[:240]})
    return result


def main(argv=None):
    """Offline stdin boundary. No paths, plant advancement or provider calls."""
    import argparse
    import base64
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdin", action="store_true", required=True)
    parser.parse_args(argv)
    bundle = shared.parse(sys.stdin.buffer.read(MAXIMUM_RAW_BYTES+1))
    _require(type(bundle) is dict and set(bundle) == {"record", "expected_record_sha256", "expected_inputs", "artifacts",
        "host_records", "expected_host_sha256", "artifact_refs"}, "final_saved_stdin_bundle")
    def decode(value):
        return base64.b64decode(value, validate=True)
    checked = verify_final_mission(decode(bundle["record"]), expected_record_sha256=bundle["expected_record_sha256"],
        expected_inputs=bundle["expected_inputs"], artifacts={key: decode(value) for key, value in bundle["artifacts"].items()},
        host_records={key: decode(value) for key, value in bundle["host_records"].items()},
        expected_host_sha256=bundle["expected_host_sha256"], artifact_refs=bundle["artifact_refs"])
    sys.stdout.write(json.dumps(checked, sort_keys=True, allow_nan=False)+"\n")
    return 0 if checked["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

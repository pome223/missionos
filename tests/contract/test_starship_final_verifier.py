"""Saved linking contracts and invented negatives; never a flight success."""
from copy import deepcopy
from hashlib import sha256
import socket
import subprocess

import pytest

from src.runtime import starship_final_verifier as final
from src.runtime import starship_final_shared as shared
from src.runtime import starship_sixdof as dyn, starship_physics as env
from src.runtime import starship_restored_return as returning
from tests.contract.test_starship_final_handoff import bound_fixture, actor, due_owner, dispatch_proof


@pytest.fixture(autouse=True)
def saved_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Saved final verification cannot run physics, providers or processes")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
        (returning, "advance_plant"), (returning, "control"), (socket, "socket"),
        (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def projection():
    from src.runtime.starship_final_faults import selection
    from src.runtime.starship_final_visual import FinalVisualPolicy
    from src.runtime.starship_final_prefix_supervision import FinalPrefixSupervisionPolicy
    from src.runtime.starship_final_sensor_execution import FinalSensorExecutionPolicy
    from dataclasses import asdict
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    from tests.contract.test_starship_restored_native_verifier import operations
    b = bound_fixture()
    source = shared.source_manifest(extra=(shared.PROFILE, shared.SITES, shared.GOAL))
    # This invented fixture retains the historical outer-process v1 contract.
    # Current timing-aware source is exercised explicitly in the v2 tests below.
    source.pop("src/runtime/starship_final_timing.py", None)
    return {"schema": final.INPUT_SCHEMA, "context": {"run_id": "final-test", "mission_id": "synthetic", "vehicle_id": "ship"},
        "plan_sha256": "a"*64, "case_id": "synthetic-linking-only", "mode": "fixture", "source_commit": "c"*40,
        "source_sha256": source, "backend": shared.environment(),
        "input_sha256": {name: source[name] for name in (shared.PROFILE, shared.SITES, shared.GOAL)},
        "profile": shared.read(shared.relative(shared.PROFILE)), "site_catalog": shared.read(shared.relative(shared.SITES)),
        "goal": shared.read(shared.relative(shared.GOAL)), "ascent_settings": b.origin.record()["ascent_settings"],
        "control_settings": b.origin.record()["booster_control_settings"], "integration_policy": RestoredIntegrationPolicy(2).record(),
        "operations": operations(), "visual_policy": FinalVisualPolicy().record(),
        "sensor_policy": asdict(FinalSensorExecutionPolicy()),
        "supervision_policy": FinalPrefixSupervisionPolicy().record(),
        "bounds": {"prefix_horizon_s": 7500., "total_horizon_s": 16000.},
        "authority": {"operator_principal_type": "test_script"}, "fault_selection": selection(1),
        "return_context_projection": False, "return_review_policy": None}


def test_execution_binding_is_derived_from_explicit_external_projection_not_producer_status(projection):
    before = shared.wire(projection)
    bound = final.execution_binding(projection)
    assert bound["schema"] == "missionos.starship_final_execution.v2"
    assert bound["verification_inputs_sha256"] == shared.digest(projection)
    assert bound["profile_sha256"] == projection["input_sha256"][shared.PROFILE]
    assert bound["settings_sha256"] == shared.digest(projection["ascent_settings"])
    assert shared.wire(projection) == before
    assert not any(name in bound for name in ("passed", "physical_success", "ai_control_completed"))


@pytest.mark.parametrize("change", ["extra", "schema", "missing", "mode", "commit", "backend", "source",
    "path", "profile_pin", "integration", "ascent", "control", "fault", "projection"])
def test_external_projection_cannot_be_loosened_or_inferred_from_a_result(projection, change):
    if change == "extra":
        projection["producer_passed"] = True
    elif change == "schema":
        projection["schema"] = final.SCHEMA
    elif change == "missing":
        del projection["authority"]
    elif change == "mode":
        projection["mode"] = "live_fixture"
    elif change == "commit":
        projection["source_commit"] = "main"
    elif change == "backend":
        projection["backend"]["executable_sha256"] = "not-pinned"
    elif change == "source":
        projection["source_sha256"] = {"passed": True}
    elif change == "path":
        projection["input_sha256"]["../private.json"] = "a"*64
    elif change == "profile_pin":
        projection["input_sha256"][shared.PROFILE] = "b"*64
    elif change == "integration":
        projection["integration_policy"]["subdivisions"] = 4
    elif change == "ascent":
        projection["ascent_settings"]["qualified_return"] = True
    elif change == "control":
        projection["control_settings"]["terminal_horizontal_braking"] = 1
    elif change == "fault":
        projection["fault_selection"]["selected_family"] = "chosen_after_result"
    else:
        projection["return_context_projection"] = 1
    with pytest.raises(ValueError):
        final.validate_verification_inputs(projection)


def hosts(inputs):
    rows = {name: {"schema": final.HOST_SCHEMA, "channel": name,
        "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"]),
        "context": inputs["context"], "mode": inputs["mode"], "record": {"synthetic_component_only": True}}
        for name in final.HOST_CHANNELS}
    raw = {name: shared.wire(value) for name, value in rows.items()}
    return rows, raw, {name: sha256(value).hexdigest() for name, value in raw.items()}


def test_separately_pinned_host_wrappers_establish_only_content_binding(projection):
    rows, raw, pins = hosts(projection)
    result = final.verify_host_wrappers(raw, pins, projection)
    assert result == {name: row["record"] for name, row in rows.items()}
    assert all("native_inference" not in component for component in result.values())


@pytest.mark.parametrize("change", ["pin", "missing", "unknown", "channel", "source", "plan", "mode", "context", "extra", "duplicate"])
def test_producer_cannot_rebind_host_transcript_or_supply_a_self_passed_receipt(projection, change):
    rows, raw, pins = hosts(projection)
    target = "sensor"
    if change == "pin":
        pins[target] = "0"*64
    elif change == "missing":
        del raw[target]
    elif change == "unknown":
        raw["unused"] = b"{}"
    elif change == "duplicate":
        raw[target] = b'{"schema":"x","schema":"y"}'
        pins[target] = sha256(raw[target]).hexdigest()
    else:
        if change == "extra":
            rows[target]["passed"] = True
        else:
            rows[target][change] = "forged"
        raw[target] = shared.wire(rows[target])
        pins[target] = sha256(raw[target]).hexdigest()
    with pytest.raises(ValueError):
        final.verify_host_wrappers(raw, pins, projection)


@pytest.fixture
def handoff_case():
    b = bound_fixture()
    execution = actor(b)
    _, _, snapshot = due_owner(execution)
    token = execution.handoff_actual_return(b.policy, dispatch=dispatch_proof(b, snapshot))
    record = token.record()
    governance = {"final_handoff": deepcopy(record), "dispatch": deepcopy(record["dispatch"]), "final_handoff_telemetry_sequence": 1}
    host = {"telemetry": [{"frame": {"sequence": 1, "actual_state": deepcopy(snapshot["state"]),
        "vehicle_sha256": shared.digest(snapshot["vehicle"]), "return_controller_started": False}}]}
    inputs = {"plan_sha256": "a"*64, "source_sha256": b.source}
    return b, record, governance, host, inputs


def check_handoff(values):
    b, record, governance, host, inputs = values
    return final.verify_handoff_record(record, governance, b.origin, b.origin, inputs=inputs, host=host)


def test_saved_handoff_links_exact_actual_point_and_inventory_without_recreating_ownership(handoff_case):
    result = check_handoff(handoff_case)
    assert result["same_actual_state_and_actuators"] is result["receipt_chain_preserved"] is True
    assert result["source_authentication_established"] is result["physical_reexecution"] is False
    assert result["handoff_sha256"] == shared.digest(handoff_case[1])


@pytest.mark.parametrize("change", ["hash", "schema", "extra", "inventory", "origin", "source", "completed", "grant",
    "sequence", "host_state", "host_vehicle", "state_missing", "actuator", "history", "future", "same_time_reset",
    "all_producer_points", "initial_binding", "reservation_hash"])
def test_saved_handoff_tampering_does_not_become_runtime_truth(handoff_case, change):
    b, row, governance, host, inputs = handoff_case
    if change == "hash":
        row["handoff_sha256"] = "0"*64
    elif change == "schema":
        row["schema"] = "missionos.starship_restored_return_prediction.v1"
    elif change == "extra":
        row["passed"] = True
    elif change == "inventory":
        row["inventory"]["release_receipts"][0]["time_s"] += .01
    elif change == "origin":
        row["origin"]["retained_count"] = 26
    elif change == "source":
        inputs["plan_sha256"] = "f"*64
    elif change == "completed":
        row["completed_flight"] = True
    elif change == "grant":
        row["mission_authority_granted"] = True
    elif change == "sequence":
        governance["final_handoff_telemetry_sequence"] = 2
    elif change == "host_state":
        host["telemetry"][-1]["frame"]["actual_state"]["omega_body_rad_s"][0] += .0001
    elif change == "host_vehicle":
        host["telemetry"][-1]["frame"]["vehicle_sha256"] = "0"*64
    elif change == "state_missing":
        del row["snapshot"]["state"]["engine_states"]
    elif change == "actuator":
        row["snapshot"]["state"]["engine_states"][0]["throttle"] = 0.
    elif change == "history":
        row["coast_history"] = row["coast_history"][-1:]
    elif change == "future":
        row["snapshot"]["state"]["time_s"] += 1.
    elif change == "same_time_reset":
        altered = deepcopy(row["coast_history"][-1])
        altered["state"]["flap_angles_rad"][0] += .001
        row["coast_history"].append(altered)
    elif change == "all_producer_points":
        row["snapshot"]["state"]["r_eci_m"][0] += 1.
        row["coast_history"][-1]["state"] = deepcopy(row["snapshot"]["state"])
        row["dispatch"]["actual_snapshot"] = deepcopy(row["snapshot"])
        governance["dispatch"] = deepcopy(row["dispatch"])
    elif change == "initial_binding":
        row["initial_binding"]["source_sha256"] = "0"*64
    else:
        row["reservation_changes"] = [{"reservation_change_sha256": "0"*64}]
    if change != "hash":
        row["handoff_sha256"] = shared.digest({key: value for key, value in row.items() if key != "handoff_sha256"})
        governance["final_handoff"] = deepcopy(row)
    with pytest.raises((ValueError, KeyError)):
        check_handoff(handoff_case)


def test_current_source_and_backend_are_recomputed_not_read_from_producer_flags(projection, monkeypatch):
    inspected = []
    actual_backend = deepcopy(projection["backend"])
    monkeypatch.setattr(shared, "check_manifest", lambda row: inspected.append(deepcopy(row)))
    monkeypatch.setattr(shared, "environment", lambda: deepcopy(actual_backend))
    result = final.verify_current_binding(projection)
    assert inspected == [projection["source_sha256"], projection["input_sha256"]]
    assert result["source_bytes_match"] is result["backend_matches"] is True
    assert result["source_commit_label_authenticated"] is False
    projection["backend"]["scipy"] = "not-installed"
    with pytest.raises(ValueError, match="backend_changed"):
        final.verify_current_binding(projection)


def rpc_pair(inputs, *, expired=False):
    """Invented framing only, never a provider or physical outcome."""
    from src.runtime import starship_final_protocol as protocol
    from src.runtime.starship_final_sensor_execution import PUBLIC_SCHEMA, CONTINUE, EFFECTS
    observation = {"schema": PUBLIC_SCHEMA, "run_id": "final-test", "request_id": "final-test-initial-1",
        "stage": "initial", "issued_at_s": 100., "deadline_s": 135., "scope_sha256": "f"*64,
        "facts": {"time_s": 100., "accel_factor": 1., "accel_band_low": .99, "accel_band_high": 1.01,
            "application_time_s": 700.}, "allowed_actions": [CONTINUE], "effects": {CONTINUE: EFFECTS[CONTINUE]}}
    request = protocol.request(observation["request_id"], "sensor", 100., 135., observation)
    binding = {"plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"])}
    frame = {"schema": protocol.REQUEST, **binding, "sequence": 1, "request": request,
        "request_sha256": shared.digest(request), "started_wall_s": 50., "wall_deadline_s": 85.}
    result = {"status": "expired" if expired else "unavailable", "action": None}
    response = {"schema": protocol.RESPONSE, **binding, "sequence": 1, "request_sha256": shared.digest(request),
        "result": result, "result_sha256": shared.digest(result)}
    client = {"schema": "missionos.starship_final_rpc_client.v1", **binding,
        "completed": [{"frame": deepcopy(frame), "response": deepcopy(response), "expired": expired,
            "delivered_result": None if expired else deepcopy(result), "proposal_only": True, "execution_permission_granted": False}],
        "pending": None, "events": [], "provider_calls_by_worker": 0, "execution_permission_granted": False}
    host = {"schema": "missionos.starship_final_rpc_host.v1", **binding,
        "sessions": [{"frame": deepcopy(frame), "delivered_result": deepcopy(result), "expired": expired,
            "handler_finished": True}], "execution_permission_granted": False}
    return client, host


@pytest.mark.parametrize("expired", [False, True])
def test_saved_rpc_joins_external_pipe_frames_without_promoting_unavailable_or_expired_to_work(projection, expired):
    client, host = rpc_pair(projection, expired=expired)
    checked = final.verify_rpc(client, host, projection)
    assert len(checked) == 1
    assert checked["final-test-initial-1"]["client"]["expired"] is expired
    assert checked["final-test-initial-1"]["client"]["delivered_result"] is None if expired else True


@pytest.mark.parametrize("change", ["request", "host_request", "sequence", "epoch", "result_hash", "result",
    "duplicate", "expired_adopted", "permission", "worker_calls", "binding", "pending"])
def test_saved_rpc_rejects_reassigned_replayed_late_or_authorizing_producer_facts(projection, change):
    client, host = rpc_pair(projection)
    row = client["completed"][0]
    if change == "request":
        row["frame"]["request"]["observation"]["facts"]["accel_factor"] = .8
    elif change == "host_request":
        host["sessions"][0]["frame"]["request"]["request_id"] = "another"
    elif change == "sequence":
        host["sessions"][0]["frame"]["sequence"] = 2
    elif change == "epoch":
        host["sessions"][0]["frame"]["wall_deadline_s"] += 1.
    elif change == "result_hash":
        row["response"]["result_sha256"] = "0"*64
    elif change == "result":
        host["sessions"][0]["delivered_result"] = {"action": "adopt"}
    elif change == "duplicate":
        client["completed"].append(deepcopy(row))
    elif change == "expired_adopted":
        row["expired"] = True
    elif change == "permission":
        client["execution_permission_granted"] = True
    elif change == "worker_calls":
        client["provider_calls_by_worker"] = 1
    elif change == "binding":
        host["source_sha256"] = "0"*64
    else:
        client["pending"] = {"frame": {"request": {"request_id": "another"}}}
    with pytest.raises(ValueError):
        final.verify_rpc(client, host, projection)


@pytest.fixture
def visual_epoch_case(projection):
    """Invented pending metadata only: no image inference or flight claim."""
    from src.runtime import starship_final_protocol as protocol, starship_final_visual as visual
    binding = {"run_id": projection["context"]["run_id"], "plan_sha256": projection["plan_sha256"],
        "source_sha256": shared.digest(projection["source_sha256"]), "mode": projection["mode"],
        "policy_sha256": shared.digest(projection["visual_policy"])}
    episode = {"episode_id": "visual-a-test", "event_id": "unconfirmed-a-test", "release_attempt_id": "a-test",
        "started_s": 2107.4500000001, "deadline_s": 2142.4500000001,
        "started_wall_s": 1288618.123456, "wall_deadline_s": 1288653.123456}
    envelopes, exchanges = [], {}
    for index, snapshot in enumerate((episode["started_s"], episode["started_s"]+2.5), 1):
        manifest = {"schema": "missionos.starship_visual_evidence.v1", "run_id": binding["run_id"],
            "vehicle_id": "ship", "snapshot_time_s": snapshot, "frames": []}
        manifest["manifest_sha256"] = shared.digest(manifest)
        request = {"schema": "missionos.starship_visual_request.v1", "run_id": binding["run_id"],
            "request_id": episode["episode_id"]+f"-{index}", "episode_id": episode["episode_id"],
            "event_id": episode["event_id"], "release_attempt_id": episode["release_attempt_id"],
            "vehicle_id": "ship", "authority_sha256": shared.digest(binding), "state_generation": 13,
            "evidence_kind": "visual" if index == 1 else "numeric_followup", "snapshot_time_s": snapshot,
            "deadline_s": episode["deadline_s"], "manifest": manifest,
            "telemetry": {"time_s": snapshot, "hold_expires_at_s": episode["deadline_s"]},
            "allowed_actions": ["stop_deployment"], "observation_choices": ["none"],
            "prior_diagnosis": None if index == 1 else {"diagnosis_id": "d"*64}}
        envelope = {"schema": visual.ENVELOPE, **binding, "request": request,
            "request_sha256": shared.digest(request), "frame_refs": [],
            "started_wall_s": episode["started_wall_s"], "wall_deadline_s": episode["wall_deadline_s"]}
        envelope["envelope_sha256"] = shared.digest(envelope)
        envelopes.append(envelope)
        rpc_request = protocol.request(request["request_id"], "visual", episode["started_s"],
            episode["deadline_s"], envelope)
        exchanges[request["request_id"]] = {"host": {"frame": {"request": rpc_request,
            "started_wall_s": episode["started_wall_s"], "wall_deadline_s": episode["wall_deadline_s"]}}, "client": None}
    value = {"schema": visual.SCHEMA, **binding, "policy": projection["visual_policy"], "episode": episode,
        "requests": envelopes, "results": [], "observation_requests": [{"action": "request_numeric_confirmation"}],
        "events": [{"event": "precautionary_hold", "time_s": episode["started_s"]}],
        "counter_attempts": [], "counter_receipts": [], "closed_reason": None,
        "native_judgments": 0, "ai_visual_work_completed": False,
        "inventory_assigned": False, "plant_state_assigned": False, "automatic_retry": False}
    host = {"schema": "missionos.starship_final_visual_host.v1", **binding, "attempts": [],
        "automatic_retry": False, "inventory_or_execution_authority_granted": False}
    return value, host, exchanges, projection


def check_visual_epoch(case):
    value, host, exchanges, inputs = case
    return final.verify_visual(value, inputs=inputs, host=host, rpc=exchanges,
        artifacts={}, inventory={"release_receipts": [], "released_count": 0, "retained_count": 26},
        # Epoch-only invented fixture; actual observation/rigid-release joins
        # have their own producer-driven tests and the aggregate prefix gate.
        prefix={"satellites": [], "concurrent_execution": {"schema": "missionos.ship_booster_clock.v1",
            "observers_run_before_each_body_step": True, "posthoc_ordering": False,
            "tick_order": [{"sequence": 0, "body_id": "ship", "phase": "stack_ascent",
                "time_s": 0., "next_time_s": 0.25, "released_count": 0}]}})


def test_saved_visual_followup_uses_original_sim_and_wall_epoch_without_native_claim(visual_epoch_case):
    value, _, exchanges, _ = visual_epoch_case
    first, followup = value["requests"]
    assert followup["request"]["snapshot_time_s"] > first["request"]["snapshot_time_s"]
    assert all(row["host"]["frame"]["request"]["time_s"] == value["episode"]["started_s"]
        for row in exchanges.values())
    result = check_visual_epoch(visual_epoch_case)
    assert result["native_judgments"] == result["fixture_judgments"] == 0
    assert result["image_semantic_truth_established"] is False


@pytest.mark.parametrize("change", ["missing_episode", "episode_deadline", "episode_wall_start", "episode_span",
    "hold_event", "snapshot_before_start", "snapshot_at_deadline", "request_deadline", "episode_id", "event_id",
    "attempt_id", "envelope_wall_start", "envelope_wall_deadline", "rpc_followup_start", "rpc_deadline",
    "rpc_wall_start", "rpc_wall_deadline", "rpc_domain", "rpc_id", "rpc_extra"])
def test_saved_visual_followup_cannot_renew_or_reassign_original_epoch(visual_epoch_case, change):
    value, _, exchanges, _ = visual_epoch_case
    envelope = value["requests"][1]
    request, episode = envelope["request"], value["episode"]
    frame = exchanges[request["request_id"]]["host"]["frame"]
    if change == "missing_episode":
        value["episode"] = None
    elif change == "episode_deadline":
        episode["deadline_s"] += 2.5
        episode["wall_deadline_s"] += 2.5
    elif change == "episode_wall_start":
        episode["started_wall_s"] += 2.5
        episode["wall_deadline_s"] += 2.5
    elif change == "episode_span":
        episode["wall_deadline_s"] += 1e-6
    elif change == "hold_event":
        value["events"][0]["time_s"] += 2.5
    elif change in ("snapshot_before_start", "snapshot_at_deadline"):
        request["snapshot_time_s"] = episode["started_s"]-1 if change == "snapshot_before_start" else episode["deadline_s"]
        request["manifest"]["snapshot_time_s"] = request["snapshot_time_s"]
        request["manifest"]["manifest_sha256"] = shared.digest({k: v for k, v in request["manifest"].items() if k != "manifest_sha256"})
    elif change == "request_deadline":
        request["deadline_s"] += 2.5
    elif change in ("episode_id", "event_id"):
        request[change] = "different-event"
    elif change == "attempt_id":
        request["release_attempt_id"] = "different-attempt"
    elif change.startswith("envelope_wall_"):
        envelope["started_wall_s" if change.endswith("start") else "wall_deadline_s"] += 2.5
    elif change == "rpc_followup_start":
        frame["request"]["time_s"] = request["snapshot_time_s"]
    elif change == "rpc_deadline":
        frame["request"]["deadline_s"] += 2.5
    elif change.startswith("rpc_wall_"):
        frame["started_wall_s" if change.endswith("start") else "wall_deadline_s"] += 2.5
    elif change == "rpc_domain":
        frame["request"]["domain"] = "sensor"
    elif change == "rpc_id":
        frame["request"]["request_id"] = "different-rpc"
    else:
        exchanges["extra"] = deepcopy(exchanges[request["request_id"]])
    # Rehash changed metadata: a valid hash must not conceal a renewed epoch.
    envelope["request_sha256"] = shared.digest(request)
    envelope["envelope_sha256"] = shared.digest({k: v for k, v in envelope.items() if k != "envelope_sha256"})
    with pytest.raises(ValueError):
        check_visual_epoch(visual_epoch_case)


def test_saved_untransmitted_visual_followup_still_keeps_original_epoch(visual_epoch_case):
    value, _, exchanges, _ = visual_epoch_case
    del exchanges[value["requests"][1]["request"]["request_id"]]
    assert check_visual_epoch(visual_epoch_case)["native_judgments"] == 0


def outer_artifacts(inputs):
    value = {"schema": "missionos.starship_final_outer_process.v1", "plan_sha256": inputs["plan_sha256"],
        "source_sha256": shared.digest(inputs["source_sha256"]), "worker_pid": 123,
        "worker_exit_code": 0, "observed_children": [], "observed_child_groups_absent": True,
        "all_children_exhaustively_observed": False, "os_attestation": False}
    raw = shared.wire(value)
    identity = sha256(raw).hexdigest()
    return value, {identity: raw}, {"outer-process.json": identity}


def test_outer_process_byte_pin_consistency_never_claims_authentication_or_exhaustive_census(projection):
    _, artifacts, refs = outer_artifacts(projection)
    checked = final.verify_numerical_tasks([], None, inputs=projection, artifacts=artifacts, artifact_refs=refs)
    assert checked["owned_task_count"] == checked["raw_numerical_results_checked"] == 0
    assert checked["os_attestation"] is checked["process_authentication_established"] is False


@pytest.mark.parametrize("change", ["missing", "path", "hash", "plan", "source", "exit", "absence", "attestation", "census", "task"])
def test_outer_process_checker_rejects_worker_self_flags_and_external_pin_reassignment(projection, change):
    value, artifacts, refs = outer_artifacts(projection)
    tasks = []
    if change == "missing":
        refs = {}
    elif change == "path":
        refs["../secret.json"] = next(iter(artifacts))
    elif change == "hash":
        artifacts[next(iter(artifacts))] = b"forged"
    elif change == "task":
        tasks = [{"passed": True}]
    else:
        field, changed = {"plan": ("plan_sha256", "0"*64), "source": ("source_sha256", "0"*64),
            "exit": ("worker_exit_code", 1), "absence": ("observed_child_groups_absent", False),
            "attestation": ("os_attestation", True), "census": ("all_children_exhaustively_observed", True)}[change]
        value[field] = changed
        raw = shared.wire(value)
        identity = sha256(raw).hexdigest()
        artifacts, refs = {identity: raw}, {"outer-process.json": identity}
    with pytest.raises(ValueError):
        final.verify_numerical_tasks(tasks, None, inputs=projection, artifacts=artifacts, artifact_refs=refs)


@pytest.mark.parametrize("status", ["accepted", "received"])
def test_saved_task_cannot_adopt_response_outside_original_wall_epoch(status):
    for received in (99., 135., 136.):
        with pytest.raises(ValueError):
            final._session_epoch({"status": status, "started_wall_s": 100., "wall_deadline_s": 135.,
                "received_wall_s": received}, 35.)


@pytest.mark.parametrize("raw", [b'{}', b'{"schema":"missionos.starship_final_native_mission.v2","passed":true}',
    b'{"schema":"missionos.starship_restored_native_mission.v1","qualification_refreshed":true}',
    b'{"passed":true,"passed":false}'])
def test_aggregate_refuses_self_promoted_boolean_and_legacy_completed_flight(raw, projection):
    _, host_raw, host_pins = hosts(projection)
    checked = final.verify_final_mission(raw, expected_record_sha256=sha256(raw).hexdigest(), expected_inputs=projection,
        artifacts={}, host_records=host_raw, expected_host_sha256=host_pins, artifact_refs={})
    assert checked["passed"] is False and checked["issues"]
    assert checked["plant_steps"] == checked["model_calls"] == 0
    assert checked["physical_execution"] is checked["qualification_refreshed"] is checked["runtime_admission"] is False


def saved_rollout(monkeypatch):
    """Use explicitly handcrafted algebraic guard evidence, never a flight."""
    from tests.contract.test_starship_final_sensor_guard import bundle_fixture
    from src.runtime import starship_final_sensor_prediction as prediction
    from src.runtime import starship_ship_capability_observation as obs
    bundle, _ = bundle_fixture(monkeypatch)
    forecast = bundle["forecasts"][0]
    state, nominal, _ = prediction.validate_snapshot(bundle["snapshot"])
    actual = obs.model_vehicle(nominal, forecast["sampled_factor"])
    return deepcopy(forecast["record"]), nominal, actual, bundle["snapshot"]["state"]


def test_actual_rollout_helper_checks_algebraic_fixture_only_without_propagation(monkeypatch):
    args = saved_rollout(monkeypatch)
    result = final._actual_rollout_checks(*args)
    assert result["record_valid"] is True
    assert result["runtime_admission"] is False


@pytest.mark.parametrize("termination,error", [("execution_wall_budget", None),
    ("numerical_or_control_failure", {"type": "ValueError", "detail": "synthetic_control_refusal"})])
def test_truthful_budget_or_control_failure_stays_a_negative_without_invented_contact(monkeypatch, termination, error):
    record, nominal, actual, initial = saved_rollout(monkeypatch)
    record["outcome"]["termination"] = termination
    record["outcome"]["contact_receipt"] = None
    record["error"] = error
    result = final._actual_rollout_checks(record, nominal, actual, initial)
    assert result["record_valid"] is result["contact_limits_met"] is False
    assert result["contact"] is None
    assert result["checks"]["terminal_contact_retained"] is False


@pytest.mark.parametrize("change", ["initial", "limits", "route", "audit_hash", "actuator", "last", "model_calls", "physical"])
def test_actual_rollout_helper_rejects_substituted_state_capability_or_trace(monkeypatch, change):
    record, nominal, actual, initial = saved_rollout(monkeypatch)
    if change == "initial":
        record["initial_state"]["propellant_kg"] -= 1.
    elif change == "limits":
        record["plant_engine_limits_n"][0] += 1.
    elif change == "route":
        record["control_capability_audit"]["all_boundary_calls"][0]["boundary"] = "plant"
    elif change == "audit_hash":
        record["control_capability_audit"]["trace_sha256"] = "0"*64
    elif change == "actuator":
        record["samples"][0]["engine_states"] = []
    elif change == "last":
        record["final_state"]["omega_body_rad_s"][0] += .01
    elif change == "model_calls":
        record["model_calls"] = 1
    else:
        record["physical_execution"] = True
    with pytest.raises(ValueError):
        final._actual_rollout_checks(record, nominal, actual, initial)


def guard_task_artifacts(inputs):
    """Invented process-file linking, not an observed process or valid guard."""
    import gzip
    value, artifacts, refs = outer_artifacts(inputs)
    value["observed_children"] = [{"pid": 456, "ppid": 123, "pgid": 456,
        "first_seen_wall_s": 101., "last_seen_wall_s": 102.}]
    def add(name, raw):
        identity = sha256(raw).hexdigest()
        artifacts[identity], refs[name] = raw, identity
        return identity
    add("outer-process.json", shared.wire(value))
    payload = {"bundle": {"synthetic_linking_only": True}, "authority": {"execution_permission": False}}
    job = {"schema": "missionos.starship_final_numerical_job.v1", "kind": "guard", "payload": payload}
    job_hash = add("numerical-0-guard/0/job.json", shared.wire(job))
    ready = {"schema": "missionos.starship_final_numerical_process_ready.v1", "job_sha256": job_hash,
        "pid": 456, "ppid": 123, "pgid": 456}
    add("numerical-0-guard/0/process-ready.json", shared.wire(ready))
    add("numerical-0-guard/0/parent-observed.json", shared.wire({**ready,
        "schema": "missionos.starship_final_numerical_parent_observation.v1", "plan_sha256": inputs["plan_sha256"],
        "outer_host_pid": 124, "observed_wall_s": 101.5}))
    receipt = {"synthetic_only": True, "passed": False}
    result = {"schema": "missionos.starship_final_numerical_result.v1", "kind": "guard", "job_sha256": job_hash,
        "checker_pid": 456, "credential_free": True, "result": receipt}
    add("numerical-0-guard/0/result.json.gz", gzip.compress(shared.wire(result), mtime=0))
    add("numerical-0-guard/0/worker.log", b"")
    tasks = [{"schema": "missionos.starship_final_numerical_task_owner.v1", "kind": "guard",
        "events": [{"event": "started", "index": 0, "pid": 456, "time": 100.},
            {"event": "reaped", "index": 0, "pid": 456, "time": 103., "exit_code": 0}],
        "cancelled": False, "maximum_concurrent_children": 1, "automatic_retry": False, "credential_free": True}]
    sensor = {"forecast_batches": [], "guard_sessions": [{**payload, "status": "received", "response": {
        "receipt": receipt, "process_evidence": {"checker_pid": 456, "job_sha256": job_hash,
            "exit_code": 0, "credential_free": True, "fixture_only": False}}}]}
    return tasks, sensor, artifacts, refs


def test_numerical_process_content_join_remains_unattested_and_does_not_infer_guard_success(projection):
    tasks, sensor, artifacts, refs = guard_task_artifacts(projection)
    result = final.verify_numerical_tasks(tasks, sensor, inputs=projection, artifacts=artifacts, artifact_refs=refs)
    assert result["guard_child_parent_group_consistent"] == result["raw_numerical_results_checked"] == 1
    assert result["process_authentication_established"] is result["physical_reexecution"] is False
    assert "guard_passed" not in result


@pytest.mark.parametrize("change", ["pid", "job_hash", "result_hash", "receipt", "observed", "retry", "reap", "missing_log",
    "missing_ready", "ack_job", "ack_pid", "ack_plan", "ack_time", "ack_principal"])
def test_numerical_process_rejects_producer_metadata_without_external_raw_join(projection, change):
    tasks, sensor, artifacts, refs = guard_task_artifacts(projection)
    if change == "pid":
        sensor["guard_sessions"][0]["response"]["process_evidence"]["checker_pid"] = 999
    elif change == "job_hash":
        sensor["guard_sessions"][0]["response"]["process_evidence"]["job_sha256"] = "0"*64
    elif change == "result_hash":
        artifacts[refs["numerical-0-guard/0/result.json.gz"]] = b"changed"
    elif change == "receipt":
        sensor["guard_sessions"][0]["response"]["receipt"] = {"passed": True}
    elif change == "observed":
        value = shared.parse(artifacts[refs["outer-process.json"]])
        value["observed_children"] = []
        raw = shared.wire(value)
        identity = sha256(raw).hexdigest()
        artifacts[identity], refs["outer-process.json"] = raw, identity
    elif change == "retry":
        tasks[0]["events"].append(deepcopy(tasks[0]["events"][0]))
    elif change == "reap":
        tasks[0]["events"] = tasks[0]["events"][:1]
    elif change == "missing_log":
        del refs["numerical-0-guard/0/worker.log"]
    elif change == "missing_ready":
        del refs["numerical-0-guard/0/process-ready.json"]
    else:
        key, value = {"ack_job": ("job_sha256", "0"*64), "ack_pid": ("pid", 999),
            "ack_plan": ("plan_sha256", "0"*64), "ack_time": ("observed_wall_s", 104.),
            "ack_principal": ("outer_host_pid", 123)}[change]
        name = "numerical-0-guard/0/parent-observed.json"
        ack = shared.parse(artifacts[refs[name]])
        ack[key] = value
        raw = shared.wire(ack)
        identity = sha256(raw).hexdigest()
        artifacts[identity], refs[name] = raw, identity
    with pytest.raises(ValueError):
        final.verify_numerical_tasks(tasks, sensor, inputs=projection, artifacts=artifacts, artifact_refs=refs)


def compact_guard():
    forecasts = [{"synthetic_opaque_batch": True}]
    snapshot = {"snapshot_id": "a"*64, "synthetic_only": True}
    sensor = {"forecast_batches": [{"snapshot": snapshot, "forecasts": forecasts}]}
    session = {"bundle": {"snapshot": deepcopy(snapshot), "forecasts_sha256": shared.digest(forecasts),
        "forecast_batch_snapshot_id": "a"*64, "synthetic_checker_field": "retained"}}
    return session, sensor


def test_compact_guard_reconstructs_exact_full_preimage_without_a_second_saved_trajectory_copy():
    session, sensor = compact_guard()
    checked = final.expanded_guard_bundle(session, sensor)
    assert checked == {"snapshot": sensor["forecast_batches"][0]["snapshot"],
        "forecasts": sensor["forecast_batches"][0]["forecasts"], "synthetic_checker_field": "retained"}
    assert "forecasts" not in session["bundle"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "digest", "snapshot", "full_copy", "batch"])
def test_compact_guard_rejects_batch_reassignment_even_when_producer_embeds_a_passed_flag(change):
    session, sensor = compact_guard()
    if change == "missing":
        sensor["forecast_batches"] = []
    elif change == "duplicate":
        sensor["forecast_batches"].append(deepcopy(sensor["forecast_batches"][0]))
    elif change == "digest":
        session["bundle"]["forecasts_sha256"] = "0"*64
    elif change == "snapshot":
        session["bundle"]["snapshot"]["synthetic_only"] = False
    elif change == "full_copy":
        session["bundle"]["forecasts"] = [{"passed": True}]
    else:
        sensor["forecast_batches"][0]["forecasts"] = [{"passed": True}]
    with pytest.raises(ValueError):
        final.expanded_guard_bundle(session, sensor)


def refusal_owner_fixture():
    """Actual-coast shape with invented saved evidence, no propagated flight."""
    from tests.contract.test_starship_restored_verifier import contact_fixture
    record, origin, profile, settings, _ = contact_fixture()
    state = deepcopy(origin.record()["state"])
    sample = deepcopy(record["samples"][0])
    sample["contact"] = False
    record.update(final_state=state, samples=[sample], return_controller=None, return_start_state=None,
        events=[record["events"][0], {"event": "return_inhibited_by_governance", "time_s": state["time_s"], "reason": "dispatch_not_admitted"}])
    record["outcome"] = {"termination": "return_inhibited_by_governance", "phase": "orbital_coast",
        "integration_steps": 0, "control_ticks": 1, "contact_receipt": None,
        "contact_metrics": {"contact_observed": False, "contact_envelope_met": False, "gates": {}}}
    binding = deepcopy(record["binding"])
    snapshot = {"state": state, "vehicle": deepcopy(origin.record()["vehicle"]), "phase": "orbital_coast",
        "binding": binding, "return_controller": None, "completed": True}
    owner = {"schema": "missionos.starship_final_actual_refusal_owner.v1", "initial_binding": deepcopy(binding),
        "binding": binding, "reservation_changes": [], "coast_history": [{"state": state, "retained_count": origin.record()["retained_count"]}],
        "actual_snapshot": snapshot, "continuation": record, "completed_v1_flight": False, "contact": None,
        "new_execution_authority_granted": False}
    dispatch = deepcopy(snapshot)
    dispatch["completed"] = False
    gov = {"schema": "missionos.starship_final_refused_governance.v1", "stage": "inhibited", "completed_v1_flight": False,
        "dispatch": {"gate_met": False, "refusal_reason": "dispatch_not_admitted", "actual_snapshot": dispatch}}
    host = {"telemetry": [{"frame": {"actual_state": deepcopy(state), "vehicle_sha256": shared.digest(snapshot["vehicle"]),
        "return_controller_started": False}}]}
    return owner, gov, origin, binding, {"profile": profile}, settings, host


def test_refused_owner_remains_consistent_unstarted_negative_without_completed_flight_or_contact():
    owner, gov, origin, binding, inputs, settings, host = refusal_owner_fixture()
    result = final.verify_refusal_owner(owner, gov, origin, binding, binding, inputs=inputs, settings=settings, host=host)
    assert result["saved_unstarted_actual_coast_consistent"] is True
    assert result["contact"] is None and result["ship_contact_limits_met"] is result["completed_v1_flight"] is False
    assert result["physical_reexecution"] is False


@pytest.mark.parametrize("change", ["source_stop", "budget_stop", "unknown", "stage", "gate", "snapshot", "contact",
    "controller", "history", "host", "completed", "authority", "binding"])
def test_refusal_cannot_hide_source_budget_failure_or_install_borrowed_state_contact_or_authority(change):
    owner, gov, origin, binding, inputs, settings, host = refusal_owner_fixture()
    if change in ("source_stop", "budget_stop", "unknown"):
        gov["dispatch"]["refusal_reason"] = {"source_stop": "source_or_observation_changed",
            "budget_stop": "forecast_unavailable", "unknown": "success_after_retry"}[change]
    elif change == "stage":
        gov["stage"] = "completed"
    elif change == "gate":
        gov["dispatch"]["gate_met"] = True
    elif change == "snapshot":
        owner["actual_snapshot"]["state"]["v_eci_mps"][0] += 1.
    elif change == "contact":
        owner["continuation"]["outcome"]["contact_receipt"] = {"contact": True}
    elif change == "controller":
        owner["continuation"]["return_controller"] = {"status": "active"}
    elif change == "history":
        owner["coast_history"] = []
    elif change == "host":
        host["telemetry"] = []
    elif change == "completed":
        owner["completed_v1_flight"] = True
    elif change == "authority":
        owner["new_execution_authority_granted"] = True
    else:
        owner["initial_binding"] = {"candidate_id": "next_orbit"}
    with pytest.raises((ValueError, KeyError)):
        final.verify_refusal_owner(owner, gov, origin, binding, binding, inputs=inputs, settings=settings, host=host)


def test_legacy_saved_rejection_is_retained_as_a_structured_negative_not_an_uncaught_boundary_error(projection, monkeypatch):
    from src.runtime.starship_sixdof_verifier import _Invalid
    def rejected(*_args, **_kwargs):
        raise _Invalid("synthetic_refusal", "$.prefix.booster", "No recovery contact is established")
    monkeypatch.setattr(final, "verify_pre_dispatch", rejected)
    monkeypatch.setattr(final, "verify_rpc", lambda *_a, **_k: {})
    value = {"schema": final.SCHEMA, "execution": final.execution_binding(projection), "prefix": {},
        "governance": None, "handoff": None, "fault_harness": None, "sensor_return": None,
        "supplemental": {"supervision": {}, "visual": {}, "client": {}, "numerical_tasks": []},
        "numerical_integration": {}, "claim_limits": final.CLAIM_LIMITS}
    raw = shared.wire(value)
    _, host_raw, pins = hosts(projection)
    result = final.verify_final_mission(raw, expected_record_sha256=sha256(raw).hexdigest(), expected_inputs=projection,
        artifacts={}, host_records=host_raw, expected_host_sha256=pins, artifact_refs={})
    assert result["passed"] is False
    assert result["issues"] == [{"code": "synthetic_refusal", "path": "$.prefix.booster", "detail": "No recovery contact is established"}]
    assert "outcome" not in result and result["qualification_refreshed"] is False


def test_final_forecast_adapter_preserves_original_host_and_all_final_backend_fields(projection):
    host = {"environment": deepcopy(projection["backend"]), "decision_host": {"requests": []}}
    before = shared.wire(host)
    legacy = final._forecast_host_projection(host, projection["backend"])
    assert set(legacy["environment"]) == set(projection["backend"])-{"machine"}
    assert all(value == projection["backend"][key] for key, value in legacy["environment"].items())
    assert legacy["decision_host"] is host["decision_host"]
    assert shared.wire(host) == before


@pytest.mark.parametrize("field", ["python", "platform", "machine", "numpy", "scipy", "executable_sha256"])
@pytest.mark.parametrize("change", ["changed", "missing"])
def test_final_host_cannot_drop_or_change_any_registered_backend_field(projection, field, change):
    host = {"environment": deepcopy(projection["backend"])}
    if change == "changed":
        host["environment"][field] += "-changed"
    else:
        del host["environment"][field]
    with pytest.raises(ValueError, match="final_forecast_host_backend_changed"):
        final._forecast_host_projection(host, projection["backend"])


@pytest.mark.parametrize("field", ["python", "platform", "numpy", "scipy", "executable_sha256", "machine"])
def test_legacy_worker_environment_stays_exact_after_final_host_adapter(projection, field):
    from src.runtime import starship_restored_native_verifier as native
    from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
    host = final._forecast_host_projection({"environment": deepcopy(projection["backend"])}, projection["backend"])
    worker = deepcopy(host["environment"])
    worker[field] = "changed-or-unexpected"
    policy = RestoredIntegrationPolicy(2)
    # Only an invented job envelope is needed: the real legacy consumer must
    # reject it at the environment gate before considering any candidate.
    job = {"schema": "missionos.starship_restored_forecast_batch.v1", "source_sha256": projection["source_sha256"],
        "integration_policy": policy.record(), "prediction_only": True, "execution_initialized": False,
        "model_calls": 0, "maximum_workers": 2, "origin_proves_numerical_policy": False,
        "matching_actual_policy_requires_outer_registered_execution_binding": True, "environment": worker}
    raw = shared.wire(job)
    identity = sha256(raw).hexdigest()
    batch = {"job": {"path": "job.json", "sha256": identity}, "batch_claim": None,
        "profile": None, "origins": {}, "candidates": {}}
    with pytest.raises(ValueError, match="native_forecast_environment_changed"):
        native._forecast_batches([batch], {identity: raw}, b"{}", projection["source_sha256"], policy, None, host)


def timed_outer_artifacts(projection):
    from src.runtime.starship_final_timing import MissionWallBudget
    projection["source_sha256"]["src/runtime/starship_final_timing.py"] = "d"*64
    projection["bounds"]["per_mission_wall_s"] = 1800.
    value, artifacts, refs = outer_artifacts(projection)
    for filename, component in (("outer-timing.json", "outer"),
            ("worker-timing.json", "native_worker_before_serialization")):
        mono, utc = iter((10., 14.)), iter((1000., 1004.))
        timer = MissionWallBudget(1800., clock=lambda: next(mono), wall_clock=lambda: next(utc))
        timer.finish()
        raw = shared.wire({"component": component, "plan_sha256": projection["plan_sha256"],
            "source_sha256": shared.digest(projection["source_sha256"]), "timing": timer.record()})
        identity = sha256(raw).hexdigest()
        artifacts[identity], refs[filename] = raw, identity
    value.update(schema="missionos.starship_final_outer_process.v2", timing_sha256=refs["outer-timing.json"])
    raw = shared.wire(value);identity = sha256(raw).hexdigest()
    artifacts[identity], refs["outer-process.json"] = raw, identity
    return value, artifacts, refs


def test_timing_aware_current_source_cannot_downgrade_to_unmeasured_outer_v1(projection):
    projection["source_sha256"]["src/runtime/starship_final_timing.py"] = "d"*64
    _, artifacts, refs = outer_artifacts(projection)
    with pytest.raises(ValueError, match="requires_suspend_inclusive_timing"):
        final.verify_numerical_tasks([], None, inputs=projection, artifacts=artifacts, artifact_refs=refs)


def test_completed_timing_pairs_are_checked_from_external_artifacts(projection):
    _, artifacts, refs = timed_outer_artifacts(projection)
    checked = final.verify_numerical_tasks([], None, inputs=projection, artifacts=artifacts, artifact_refs=refs)
    assert checked["mission_timing"]["outer"]["utc_elapsed_s"] == 4.
    assert checked["mission_timing"]["native_worker_before_serialization"]["within_budget"] is True


@pytest.mark.parametrize("mutation", ["missing", "changed_bytes", "plan", "exceeded", "unclosed", "outside_outer"])
def test_timing_cannot_be_missing_tampered_late_or_unrelated(projection, mutation):
    _, artifacts, refs = timed_outer_artifacts(projection)
    name = "worker-timing.json"
    if mutation == "missing":
        del refs[name]
    elif mutation == "changed_bytes":
        artifacts[refs[name]] = b"{}"
    else:
        value = shared.parse(artifacts[refs[name]])
        if mutation == "plan": value["plan_sha256"] = "0"*64
        elif mutation == "exceeded": value["timing"]["end"]["utc_s"] += 1801.
        elif mutation == "unclosed": value["timing"]["closed"] = False
        else:
            value["timing"]["start"]["utc_s"] += 10.
            value["timing"]["end"]["utc_s"] += 10.
        raw=shared.wire(value);identity=sha256(raw).hexdigest()
        artifacts[identity], refs[name] = raw, identity
    with pytest.raises(ValueError):
        final.verify_numerical_tasks([], None, inputs=projection, artifacts=artifacts, artifact_refs=refs)

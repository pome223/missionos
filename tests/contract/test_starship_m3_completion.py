"""M3 completion is a matrix of bound evidence, not a successful single flight."""
from copy import deepcopy
import pytest
from src.runtime.starship_m3_completion import PHASES, phase_coverage
from test_starship_ascent_supervisor import setup


def test_ascent_loss_during_pending_request_is_not_forgotten(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    actor.observe(tick(1, failed=True))
    assert not actor.deployment_permission()
    actor.observe(tick(12, failed=True, withheld=True))
    assert actor.request_count == 2
    request = actor.pending["request"]
    assert request["trigger"] == "health_changed"
    assert request["observation"]["time_s"] == 12
    assert not all(request["observation"]["engine_available"])
    actor.observe(tick(24, failed=True, withheld=True))
    assert actor.request_count == 2 and actor.pending is None
    assert all(row["physical_engine_command"] is False for row in actor.records)


def test_ascent_phase_change_waiting_for_reply_is_queued(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    actor.observe(tick(1, phase="ship_ascent"))
    actor.observe(tick(12, phase="ship_ascent"))
    assert actor.pending["request"]["trigger"] == "phase_changed"
    assert actor.pending["request"]["observation"]["phase"] == "ship_ascent"


def test_ascent_initial_loss_still_requests_initial_supervision():
    actor, tick, _ = setup()
    actor.observe(tick(0, failed=True))
    assert actor.request_count == 1 and not actor.deployment_permission()
    assert actor.pending["request"]["trigger"] == "initial_monitor"
    actor.observe(tick(.1, failed=True, withheld=True))
    assert actor.records[0]["accepted_action"] == "withhold_deployment"


def test_deferred_event_cannot_expand_request_budget(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.request_count = actor.authority["maximum_requests"]
    actor.observe(tick(0))
    actor.observe(tick(1, failed=True))
    actor.observe(tick(30, failed=True, withheld=True))
    assert actor.pending is None and not actor.deployment_permission()
    assert actor.request_count == actor.authority["maximum_requests"]


def adopted(provider="typesafe"):
    response = {"mode": "live", "action": "continue", "model_inference_invoked": True,
        "jev_invocation" if provider == "typesafe" else "llm_invocation": {
            "schema_version": "runtime_invocation_evidence.v1", "mode": "live",
            "provider": provider, "status": "succeeded", "call_attempted": True,
            "model_inference_invoked": True}}
    return {"response": response, "accepted_action": "continue", "fallback": False}


def phase_study():
    return {"m2_study": {"concurrent_prefix": {"ascent": {"decisions": [adopted()]},
        "prefix_run": {"booster_mission_director": {"records": [adopted()]}}},
        "deployment_run": {"mission_director": {"records": [adopted()]}},
        "return_study": {"decisions": [adopted()]}}}


def test_normal_phase_coverage_does_not_invent_deepseek():
    result = phase_coverage(phase_study())
    assert all(result[name] for name in PHASES[:-1])
    assert result["return_deepseek"] is False


def test_adopted_deepseek_is_reported_separately():
    study = phase_study()
    study["m2_study"]["return_study"]["decisions"].append(adopted("deepseek"))
    assert all(phase_coverage(study).values())


@pytest.mark.parametrize("change", ["fallback", "fixture", "attempt_only", "not_adopted", "synthetic"])
def test_attempted_or_rejected_response_is_not_live_coverage(change):
    study = phase_study()
    row = study["m2_study"]["concurrent_prefix"]["ascent"]["decisions"][0]
    if change == "fallback": row["fallback"] = True
    elif change == "fixture": row["response"]["mode"] = "fixture"
    elif change == "attempt_only": row["response"]["jev_invocation"]["model_inference_invoked"] = False
    elif change == "not_adopted": row["accepted_action"] = None
    else: row["response"]["synthetic_response_fault"] = "hold_action"
    assert phase_coverage(study)["ascent_jev"] is False


def test_malformed_phase_evidence_never_supplies_coverage():
    assert not any(phase_coverage({}).values())


from src.runtime import starship_m3_completion as completion
from src.runtime.starship_replanning import digest

SOURCES = {"src/runtime/example.py": "a"*64}


def scope_evidence(tmp_path):
    from src.runtime.starship_inflight_approval import InflightApprovalLedger
    evidence = {"schema": "missionos.starship_m3_scope_runtime_evidence.v1", "source_sha256": SOURCES,
        "operator_response_source": "scripted_test_operator", "real_gateway_http": True,
        "fixture_worker_snapshot": True, "live_model_inference": False, "physical_execution": False, "cases": {}}
    for role, name in (("scope_reject", "reject"), ("scope_expire", "expire"), ("scope_replay", "approve")):
        clock = [100.]
        scope = {"commands": ["hold"], "resource_limits": {"hold_seconds": 15.}}
        registry = {"commands": ["hold", "resume"], "resource_limits": {"hold_seconds": 30.}}
        ledger = InflightApprovalLedger(tmp_path/(name+".sqlite"), signing_key=b"fixture-only-integrity-key-32bytes",
            operator_token="explicit-fixture-operator-token-not-secret", operator_id="fixture-operator", registry=registry, clock=lambda:clock[0])
        obs = {"state_sha256": "a"*64, "simulation_time_s": 10., "wall_time_s": clock[0]}
        request = ledger.create(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2,
            scope=scope, delta={"add_commands": ["resume"], "resource_limits": {}}, observation=obs,
            simulation_time_s=10., simulation_deadline_s=25., wall_deadline_s=110., fallback="hold")
        body = {"request_sha256": request["sha256"], "observed_state_sha256": request["observed_state_sha256"]}
        path = f"/missionos/starship/inflight/runs/{name}/missions/mission1/vehicles/ship/requests/request1/"
        if role == "scope_expire":
            clock[0] = 111.
            receipt = None
            http = [{"path": path+"approve", "method": "POST", "request_body": body,
                     "status": 409, "response_body": {"detail": "approval_deadline_expired"}, "authenticated_test_operator": True}]
        else:
            receipt = ledger.resolve(request_id="request1", **body, choice=name, bearer_token="explicit-fixture-operator-token-not-secret", simulation_time_s=10.)
            http = [{"path": path+name, "method": "POST", "request_body": body,
                     "status": 200, "response_body": {"receipt": receipt, "executor_command_issued": False}, "authenticated_test_operator": True}]
        snapshot = {"scope": scope, "observation": {**obs, "wall_time_s": clock[0]}}
        result = ledger.consume(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2,
            scope=scope, observation=snapshot["observation"], simulation_time_s=10., run_active=True)
        consumptions = [{"run_id": name, "snapshot": snapshot, "result": result}]
        if role == "scope_replay":
            http.append({"path": path+name, "method": "POST", "request_body": body, "status": 409,
                         "response_body": {"detail": "request_already_resolved"}, "authenticated_test_operator": True})
            consumptions.append({"run_id": name, "snapshot": snapshot, "error": "request_already_consumed"})
        evidence["cases"][role] = {"run_id": name, "ledger": ledger.status("request1"), "original_scope": scope,
            "registry": registry, "snapshot": snapshot, "http": http, "consumptions": consumptions}
    return evidence


def test_registered_ascent_runtime_replay_is_separate_from_real_provider_coverage():
    evidence = completion.produce_ascent_runtime_evidence(SOURCES)
    assert evidence["real_model_calls"] == evidence["physics_steps"] == 0
    assert completion.verify_ascent_runtime(evidence, expected_sources=SOURCES) == {
        "late_or_missing": True, "ascent_health_change": True}


def test_ascent_runtime_rejects_missing_deferred_observation():
    evidence = completion.produce_ascent_runtime_evidence(SOURCES)
    evidence["cases"]["delayed_health"]["output"]["record"]["decisions"].pop()
    with pytest.raises(ValueError, match="replay_mismatch"):
        completion.verify_ascent_runtime(evidence, expected_sources=SOURCES)


def test_registered_scope_records_authenticate_fixture_ledger_and_dispositions(tmp_path):
    evidence = scope_evidence(tmp_path)
    assert all(completion.verify_scope_runtime(evidence, expected_sources=SOURCES).values())


@pytest.mark.parametrize("mutation", ["signature", "no_replay", "wrong_http_run", "source"])
def test_scope_runtime_rejects_tampered_or_incomplete_evidence(tmp_path, mutation):
    evidence = scope_evidence(tmp_path)
    if mutation == "signature": evidence["cases"]["scope_reject"]["ledger"]["receipt"]["choice"] = "approve"
    elif mutation == "no_replay": evidence["cases"]["scope_replay"]["consumptions"].pop()
    elif mutation == "wrong_http_run": evidence["cases"]["scope_reject"]["http"][0]["path"] = "/other"
    else: evidence["source_sha256"] = {"src/runtime/example.py": "b"*64}
    with pytest.raises(ValueError): completion.verify_scope_runtime(evidence, expected_sources=SOURCES)


def sign(value, key):
    import hmac
    return {**value, "signature": hmac.new(key, digest(value).encode(), "sha256").hexdigest()}


@pytest.fixture
def campaign(tmp_path, monkeypatch, request):
    matrix = getattr(request, "param", "original_v1")
    import hashlib, json
    from src.runtime import starship_m3_live
    from test_starship_reference_ids import request, energy_ids
    bindings = {role: {"authority": {"case": case, "mode": "live"}, "scenario": "sixdof_m3_"+case}
                for role, case in completion.FLIGHT_MATRICES[matrix].items()}
    protocol = completion.build_protocol(bindings, source_sha256=SOURCES, return_source_sha256=SOURCES, matrix=matrix)
    flights, gateway, keys = {}, {}, {}
    for role, binding in bindings.items():
        key = keys[role] = (role*32).encode()[:32]
        study = phase_study()
        study.update(authority=binding["authority"], run_id=role)
        inner = study["m2_study"]
        inner["handoff"] = {"released_count": 0 if role == "deployment_change" else 26, "total_count": 26,
                             "history": [{"kind": "command", "action": "continue"}]}
        inner["return_study"]["dispatch"] = {"candidate_id": "nominal"}
        if role == "ambiguous_comparison":
            decision = adopted("deepseek")
            decision["response"]["action"] = decision["accepted_action"] = "keep_plan"
            decision["request"] = request()
            decision["response"]["decision_basis"] = {"reference_ids": energy_ids(decision["request"]),
                "compared_candidate_ids": ["nominal", "next_orbit"]}
            inner["return_study"]["decisions"].append(decision)
        if role == "deployment_change":
            decision = inner["deployment_run"]["mission_director"]["records"][0]
            decision["accepted_action"] = decision["response"]["action"] = "stop_deployment"
        if role == "scope_amendment":
            decision = adopted()
            decision["accepted_action"] = decision["response"]["action"] = "select_next_orbit"
            inner["return_study"]["decisions"].append(decision)
        raw = flights[role] = json.dumps(study).encode()
        plan = {"id": role, "session_id": role, "scenario": binding["scenario"],
                "source_sha256": SOURCES, "mission_envelope": binding["authority"]}
        plan["sha256"] = digest(plan)
        approval = sign({"plan_id": role, "session_id": role, "plan_sha256": plan["sha256"],
            "mission_envelope": binding["authority"], "operator_command_received": True,
            "physical_execution_authorized": False, "created_at_epoch_s": 10., "expires_at_epoch_s": 20.,
            "consumed_by_run": role}, key)
        receipt = sign({"schema": "missionos.starship_worker_receipt.v1", "run_id": role,
            "plan_sha256": plan["sha256"], "worker_pid": 123, "verification_passed": True,
            "artifact_sha256": {"study.json": hashlib.sha256(raw).hexdigest()}, "physical_execution": False}, key)
        gateway[role] = {"plan": plan, "approval": approval, "receipt": receipt,
                        "execution": {"run_id": role, "worker_pid": 123, "started_at_epoch_s": 12.}}
    role = "scope_amendment"
    operator, host = operator_http_bundle(role)
    gateway[role]["operator_http_record"] = operator
    gateway[role]["inflight_verification_inputs"] = {"expected_host_evidence": sign(host, keys[role])}
    calls = []
    def verifier(study, **kwargs):
        calls.append(kwargs)
        assert study["run_id"] == kwargs["expected_run_id"]
        return {"passed": True, "case_accepted": True, "inflight_verification": {
            "passed": True, "scope_amendment_applied": True, "reservation_observed": True,
            "http_delivered": True, "local_operator_authenticated": True}}
    monkeypatch.setattr(starship_m3_live, "verify", verifier)
    runtime = {"scope": scope_evidence(tmp_path), "ascent": completion.produce_ascent_runtime_evidence(SOURCES)}
    kwargs = {"expected_protocol_sha256": digest(protocol), "gateway_records": gateway, "host_signing_key": keys}
    return protocol, flights, runtime, kwargs, calls


def test_four_flight_completion_checks_raw_records_current_sources_and_gateway_signatures(campaign):
    protocol, flights, runtime, kwargs, calls = campaign
    result = completion.verify_completion(protocol, flights, runtime, **kwargs)
    assert result["m3_completed"] and result["passed"]
    assert len(calls) == 4 and all(result["live_phase_coverage"].values())
    assert result["comparison_reference_audits"][0]["status"] == "valid"
    assert not result["physical_execution"] and not result["physical_fault_robustness_qualified"]


@pytest.mark.parametrize("mutation", ["source", "missing_flight", "missing_runtime", "raw_bytes", "signature", "run_identity", "model_missing"])
def test_completion_cannot_upgrade_partial_forged_or_fixture_evidence(campaign, mutation):
    import json, hashlib
    protocol, flights, runtime, kwargs, _ = campaign
    if mutation == "source": protocol["source_sha256"] = {"else": "b"*64}
    elif mutation == "missing_flight": flights.pop("scope_amendment")
    elif mutation == "missing_runtime": runtime.pop("scope")
    elif mutation == "raw_bytes": flights["normal"] += b" "
    elif mutation == "signature": kwargs["gateway_records"]["normal"]["approval"]["signature"] = "0"*64
    elif mutation == "run_identity": kwargs["gateway_records"]["normal"]["execution"]["run_id"] = "other"
    else:
        role = "ambiguous_comparison"
        study = json.loads(flights[role])
        study["m2_study"]["return_study"]["decisions"].pop()
        flights[role] = json.dumps(study).encode()
        receipt = kwargs["gateway_records"][role]["receipt"]
        receipt.pop("signature")
        receipt["artifact_sha256"]["study.json"] = hashlib.sha256(flights[role]).hexdigest()
        kwargs["gateway_records"][role]["receipt"] = sign(receipt, kwargs["host_signing_key"][role])
    result = completion.verify_completion(protocol, flights, runtime, **kwargs)
    assert not result["m3_completed"] and result["issues"]


def operator_http_bundle(run_id="scope_amendment"):
    request = {"request_id": run_id+"-return-inflight-1", "sha256": "a"*64,
               "observed_state_sha256": "b"*64, "candidate_sha256": "c"*64}
    receipt = {"choice": "approve", "local_operator_authenticated": True, "request_sha256": request["sha256"]}
    host = {"operator_response_source": "scripted_test_driver", "host_ledger_signatures_verified": True,
            "signed_ledger_record": {"request": request, "receipt": receipt}}
    base = "/missionos/starship/inflight/runs/"+run_id+"-return/missions/"+run_id+"/vehicles/ship"
    body = {"request_sha256": request["sha256"], "observed_state_sha256": request["observed_state_sha256"],
            "candidate_sha256": request["candidate_sha256"]}
    path = base+"/requests/"+request["request_id"]+"/approve"
    rows = [{"path": base+"/pending", "method": "GET", "request_body": None, "status": 200,
             "response_body": {"request": request, "resolvable": True}},
            {"path": path, "method": "POST", "request_body": body, "status": 200,
             "response_body": {"receipt": receipt, "executor_command_issued": False}},
            {"path": path, "method": "POST", "request_body": body, "status": 409,
             "response_body": {"detail": "request_already_resolved"}}]
    record = {"schema": "missionos.starship_m3_scripted_operator_http.v1", "run_id": run_id,
        "choice": "approve", "resolved": True, "physical_execution": False,
        "operator_response_source": "scripted_test_driver", "authenticated_human_identity_verified": False,
        "http": rows}
    return deepcopy(record), deepcopy(host)


@pytest.mark.parametrize("mutation", ["missing_get", "wrong_candidate", "other_receipt", "missing_replay", "claims_human"])
def test_live_operator_http_cannot_be_inferred_from_a_signed_ledger_alone(mutation):
    operator, host = operator_http_bundle()
    if mutation == "missing_get": operator["http"].pop(0)
    elif mutation == "wrong_candidate": operator["http"][1]["request_body"]["candidate_sha256"] = "f"*64
    elif mutation == "other_receipt": operator["http"][1]["response_body"]["receipt"]["request_sha256"] = "f"*64
    elif mutation == "missing_replay": operator["http"].pop()
    else: operator["authenticated_human_identity_verified"] = True
    with pytest.raises(ValueError): completion.verify_live_operator_http(operator, host=host, run_id="scope_amendment")


def test_scope_closure_matrix_is_explicit_and_old_protocol_remains_identical():
    bindings = {role: {"authority": {"case": case, "mode": "live"}, "scenario": "sixdof_m3_"+case}
                for role, case in completion.FLIGHT_CASES.items()}
    old = completion.build_protocol(bindings, source_sha256=SOURCES, return_source_sha256=SOURCES)
    assert old["schema"].endswith(".v1") and "flight_matrix" not in old
    assert old == completion.build_protocol(bindings, source_sha256=SOURCES,
        return_source_sha256=SOURCES, matrix="original_v1")
    changed = deepcopy(bindings)
    changed["scope_amendment"]["authority"]["case"] = "inflight_op_scope_01"
    changed["scope_amendment"]["scenario"] = "sixdof_m3_inflight_op_scope_01"
    with pytest.raises(ValueError, match="m3_matrix_case_mode"):
        completion.build_protocol(changed, source_sha256=SOURCES, return_source_sha256=SOURCES)
    new = completion.build_protocol(changed, source_sha256=SOURCES,
        return_source_sha256=SOURCES, matrix="scope_closure_v2")
    assert new["schema"].endswith(".v2") and new["flight_matrix"] == "scope_closure_v2"
    assert new["maximum_attempts_per_flight"] == old["maximum_attempts_per_flight"] == 1
    assert new["runtime_roles"] == old["runtime_roles"]
    with pytest.raises(ValueError, match="m3_unregistered_flight_matrix"):
        completion.build_protocol(changed, source_sha256=SOURCES,
            return_source_sha256=SOURCES, matrix="unreviewed")


def test_protocol_matrix_cannot_be_changed_after_approval(campaign):
    protocol, flights, runtime, kwargs, _ = campaign
    protocol["flight_matrix"] = "scope_closure_v2"
    result = completion.verify_completion(protocol, flights, runtime, **kwargs)
    assert not result["m3_completed"]
    assert result["issues"] == ["m3_matrix_approval_hash"]


@pytest.mark.parametrize("campaign", ["scope_closure_v2"], indirect=True)
def test_scope_closure_matrix_retains_full_four_flight_completion_checks(campaign):
    protocol, flights, runtime, kwargs, calls = campaign
    result = completion.verify_completion(protocol, flights, runtime, **kwargs)
    assert result["m3_completed"] and result["passed"]
    assert len(calls) == 4 and all(result["runtime_roles"].values())
    assert result["scope_http_verification"]["replay_rejected"]


@pytest.mark.parametrize("campaign", ["scope_closure_v2"], indirect=True)
def test_approved_but_inconsistent_matrix_schema_is_rejected(campaign):
    protocol, flights, runtime, kwargs, _ = campaign
    protocol["schema"] = "missionos.starship_m3_completion_protocol.v1"
    kwargs["expected_protocol_sha256"] = digest(protocol)
    result = completion.verify_completion(protocol, flights, runtime, **kwargs)
    assert not result["m3_completed"]
    assert result["issues"] == ["m3_matrix_protocol_modified"]

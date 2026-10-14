"""Completion evidence for a frozen M3 campaign, separate from a single flight.

A normal flight need not invoke DeepSeek. Provider coverage is evidence of
adopted decisions across the approved matrix, never a required action change or
an AI-versus-fallback score. These reducers must follow record verification;
receipts alone cannot authenticate a remote service or prove physical execution.
"""

PHASES = ("ascent_jev", "booster_jev", "deployment_jev", "return_jev", "return_deepseek")


def _adopted(rows, key, provider):
    for row in rows:
        if type(row) is not dict:
            continue
        response = row.get("response")
        if type(response) is not dict or response.get("mode") != "live":
            continue
        receipt = response.get(key)
        if (type(receipt) is not dict
                or receipt.get("schema_version") != "runtime_invocation_evidence.v1"
                or receipt.get("mode") != "live"
                or receipt.get("provider") != provider
                or receipt.get("status") != "succeeded"
                or receipt.get("call_attempted") is not True
                or receipt.get("model_inference_invoked") is not True
                or response.get("model_inference_invoked") is not True
                or type(response.get("action")) is not str
                or response.get("synthetic_response_fault") is not None):
            continue
        if "accepted_action" in row:
            accepted = (row["accepted_action"] == response["action"]
                        and row.get("fallback", False) is False)
        else:
            dispatch = row.get("dispatch")
            accepted = (type(dispatch) is dict and dispatch.get("rules_accepted") is True
                        and dispatch.get("action") == response["action"])
        if accepted:
            return True
    return False


def phase_coverage(study):
    """Extract adopted real-provider phase evidence; not an integrity verdict.

    Call only in conjunction with the independent M3/M2 record verifiers. An
    attempted, malformed, rejected or fallback response supplies no coverage.
    Absence of unnecessary DeepSeek inference in normal flight is legitimate.
    """
    try:
        inner = study["m2_study"]
        prefix = inner["concurrent_prefix"]
        rows = (
            prefix["ascent"]["decisions"],
            prefix["prefix_run"]["booster_mission_director"]["records"],
            inner["deployment_run"]["mission_director"]["records"],
            inner["return_study"]["decisions"],
        )
        if any(type(value) is not list for value in rows):
            raise ValueError("m3_phase_decision_rows")
        deep = prefix.get("authority", {}).get("prefix_provider_policy") == "jev_ascent_deepseek_booster_report_v1"
        phases = tuple("booster_deepseek" if deep and k == "booster_jev" else k for k in PHASES)
        return dict(zip(phases, (
            _adopted(rows[0], "jev_invocation", "typesafe"),
            _adopted(rows[1], "llm_invocation" if deep else "jev_invocation", "deepseek" if deep else "typesafe"),
            _adopted(rows[2], "jev_invocation", "typesafe"),
            _adopted(rows[3], "jev_invocation", "typesafe"),
            _adopted(rows[-1], "llm_invocation", "deepseek"),
        )))
    except (KeyError, TypeError, ValueError):
        return dict.fromkeys(PHASES, False)


FLIGHT_CASES = {"normal": "op_trial_05", "deployment_change": "release_fault",
                "scope_amendment": "inflight_op_trial_02", "ambiguous_comparison": "op_trial_07"}
FLIGHT_MATRICES = {
    "original_v1": FLIGHT_CASES,
    "scope_closure_v2": {**FLIGHT_CASES, "scope_amendment": "inflight_op_scope_01"},
}
RUNTIME_ROLES = ("late_or_missing", "ascent_health_change", "scope_reject", "scope_expire", "scope_replay")


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _copy(value):
    import json
    return json.loads(json.dumps(value, allow_nan=False))


def _sources(value):
    import re
    return (type(value) is dict and bool(value)
            and all(type(k) is str and not k.startswith(("/", "../"))
                    and type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v)
                    for k, v in value.items()))


def build_protocol(flight_bindings, *, source_sha256, return_source_sha256, matrix="original_v1"):
    """Freeze external inputs before the four declared flights; no calls/run.

    Each binding is {scenario, authority}; Gateway assigns run IDs afterward. The caller must retain and approve this
    protocol's digest outside the result artifacts. An artifact cannot approve
    itself by supplying a different expected source or run identity.
    """
    _require(type(matrix) is str and matrix in FLIGHT_MATRICES, "m3_unregistered_flight_matrix")
    cases = FLIGHT_MATRICES[matrix]
    _require(type(flight_bindings) is dict and set(flight_bindings) == set(cases), "m3_matrix_roles")
    _require(_sources(source_sha256) and _sources(return_source_sha256)
             and all(source_sha256.get(k) == v for k, v in return_source_sha256.items()), "m3_matrix_sources")
    for role, binding in flight_bindings.items():
        _require(type(binding) is dict and set(binding) == {"scenario", "authority"}, "m3_matrix_flight_binding")
        _require(type(binding["scenario"]) is str and bool(binding["scenario"])
                 and binding["authority"]["mode"] == "live"
                 and binding["authority"]["case"] == cases[role], "m3_matrix_case_mode")
    version = ({} if matrix == "original_v1" else {"flight_matrix": matrix})
    return _copy({"schema": ("missionos.starship_m3_completion_protocol.v1" if matrix == "original_v1"
                             else "missionos.starship_m3_completion_protocol.v2"), **version,
        "flight_bindings": flight_bindings, "source_sha256": source_sha256,
        "return_source_sha256": return_source_sha256, "runtime_roles": list(RUNTIME_ROLES),
        "maximum_attempts_per_flight": 1, "evaluation": "actual_mission_control_duties_not_rules_score",
        "booster_scope": "declared_water_entry_criteria_no_new_physical_perturbation_claim",
        "runtime_scope": "current_source_fixture_boundary_evidence_not_live_model_or_flight",
        "physical_execution_authorized": False})


def _signed_fixture(value):
    import hashlib
    import hmac
    from .starship_inflight_approval import _json, digest
    _require(type(value) is dict and type(value.get("signature")) is str, "scope_signed_row_required")
    body = {k: v for k, v in value.items() if k != "signature"}
    _require(body.get("sha256") == digest({k: v for k, v in body.items() if k != "sha256"})
             and hmac.compare_digest(value["signature"], hmac.new(
                 b"fixture-only-integrity-key-32bytes", _json(body).encode(), hashlib.sha256).hexdigest()),
             "scope_fixture_ledger_integrity")


def verify_scope_runtime(record, *, expected_sources):
    """Check the registered real-HTTP fixture ledger; never live-human evidence."""
    from .starship_replanning import digest
    _require(record["schema"] == "missionos.starship_m3_scope_runtime_evidence.v1"
             and record["source_sha256"] == expected_sources
             and record["operator_response_source"] == "scripted_test_operator"
             and record["real_gateway_http"] is True and record["fixture_worker_snapshot"] is True
             and record["live_model_inference"] is False and record["physical_execution"] is False
             and set(record["cases"]) == {"scope_reject", "scope_expire", "scope_replay"}, "scope_runtime_binding")
    result = {}
    for role, case in record["cases"].items():
        ledger = case["ledger"]
        request, receipt, consumption = (ledger[k] for k in ("request", "receipt", "consumption"))
        for row in (request, receipt, consumption):
            _signed_fixture(row)
        _require(request["current_scope"] == case["original_scope"] == case["snapshot"]["scope"]
                 and request["current_scope_sha256"] == digest(case["original_scope"])
                 and request["registry_sha256"] == digest(case["registry"])
                 and receipt["request_sha256"] == consumption["request_sha256"] == request["sha256"]
                 and consumption["receipt_sha256"] == receipt["sha256"]
                 and request["request_id"] == receipt["request_id"] == consumption["request_id"]
                 and consumption["scope_applied"] is False and consumption["executor_command_issued"] is False
                 and consumption["physical_execution_authorized"] is False, "scope_runtime_row_binding")
        consumed = [row for row in case["consumptions"] if "result" in row]
        _require(len(consumed) == 1 and consumed[0]["result"] == consumption
                 and consumed[0]["run_id"] == case["run_id"]
                 and consumed[0]["snapshot"]["scope"] == case["original_scope"]
                 and consumed[0]["snapshot"]["observation"] == consumption["latest_observation"], "scope_runtime_consumption_trace")
        posts = [row for row in case["http"] if row["method"] == "POST"]
        endpoint = ("/missionos/starship/inflight/runs/"+case["run_id"]+"/missions/"
                    +request["mission_id"]+"/vehicles/"+request["vehicle_id"]
                    +"/requests/"+request["request_id"]+"/")
        _require(bool(posts) and all(row["authenticated_test_operator"] is True and row["path"] in {endpoint+"approve", endpoint+"reject"}
                 and row["request_body"] == {"request_sha256": request["sha256"],
                     "observed_state_sha256": request["observed_state_sha256"]} for row in posts), "scope_runtime_http_binding")
        if role != "scope_replay":
            choice, reason, code = ("reject", "rejected", 200) if role == "scope_reject" else ("expire", "expired", 409)
            _require(receipt["choice"] == choice and consumption["approved"] is False
                     and consumption["reason"] == reason and consumption["fallback"] == "hold"
                     and consumption["scope"] == case["original_scope"]
                     and consumption["revision"] == request["current_revision"]
                     and any(row["status"] == code and row["path"] == endpoint+("reject" if choice == "reject" else "approve")
                             for row in posts), "scope_negative_disposition")
            if role == "scope_reject":
                _require(receipt["local_operator_authenticated"] is True
                         and any(row["status"] == 200 and row["response_body"].get("receipt") == receipt
                                 and row["response_body"].get("executor_command_issued") is False for row in posts),
                         "scope_reject_http_receipt")
            if role == "scope_expire":
                _require(consumption["wall_time_s"] >= request["wall_deadline_s"]
                         or consumption["simulation_time_s"] >= request["simulation_deadline_s"], "scope_expiry_before_deadline")
        else:
            _require(receipt["choice"] == "approve" and consumption["approved"] is True
                     and any(row["status"] == 200 and row["path"] == endpoint+"approve"
                             and row["response_body"].get("receipt") == receipt for row in posts)
                     and any(row["status"] == 409 for row in posts)
                     and any(row.get("error") == "request_already_consumed"
                         and row["run_id"] == case["run_id"] for row in case["consumptions"]), "scope_replay_not_rejected")
        result[role] = True
    return result


def _ascent_inputs(kind):
    from dataclasses import asdict
    from .starship_sixdof import State6DOF, EngineState
    if kind == "delayed_health":
        values = [(0., False, False), (1., True, False), (12., True, True), (24., True, True), (25., True, True)]
    elif kind == "initial_health":
        values = [(0., True, False), (12., True, True), (13., True, True)]
    elif kind == "missing_reply":
        values = [(0., False, False), (12., False, False), (13., False, False)]
    elif kind == "late_reply":
        values = [(0., False, False), (12., False, False), (13., False, False)]
    else:
        raise ValueError("unknown_ascent_boundary_case")
    return [{"body_id": "ship", "time_s": t, "phase": "stack_ascent",
             "state": asdict(State6DOF(t, (6378437., 0., 0.), (0., 0., 0.),
                 (1., 0., 0., 0.), (0., 0., 0.), 100000., (EngineState(available=not failed),))),
             "deployment_withheld": withheld, "sequencer_state": "skipped" if withheld else "running",
             "released_count": 0} for t, failed, withheld in values]


def _run_ascent_trace(kind, run_id, inputs):
    """Production actor with raw synthetic sensor ticks; no integration/API call."""
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from .starship_ascent_supervisor import AscentSupervisor, CONFIG
    from .starship_replanning import digest
    from .starship_sixdof import state_from_dict
    class Clock:
        now = 0.
        def __call__(self): return self.now
        def sleep(self, dt): self.now += max(dt, 1e-6)
    clock = Clock()
    effects = []
    with TemporaryDirectory(prefix="m3-ascent-boundary-") as temporary:
        actor = AscentSupervisor(CONFIG, mode="live", run_id=run_id,
            mailbox=Path(temporary), clock=clock, sleep=clock.sleep)
        for item in inputs:
            if kind == "late_reply" and item["time_s"] == 12. and actor.pending:
                request = actor.pending["request"]
                # Explicitly not a provider invocation. At the exact deadline
                # even a correctly bound syntactic choice cannot be adopted.
                response = {"request_id": request["request_id"], "request_sha256": digest(request),
                            "action": "withhold_deployment", "mode": "fixture", "model_inference_invoked": False}
                (Path(temporary)/"response.json").write_text(json.dumps(response))
            actor.observe(SimpleNamespace(**{**item, "state": state_from_dict(item["state"])}))
            effects.append({"time_s": item["time_s"], "deployment_permission": actor.deployment_permission()})
        return _copy({"record": actor.finish(), "effects": effects,
                      "fixture_wall_elapsed_s": clock.now})


def produce_ascent_runtime_evidence(source_sha256, run_id="m3-ascent-boundaries"):
    """Run fixed current-code actor boundaries, not a flight or real provider."""
    _require(_sources(source_sha256), "ascent_runtime_sources")
    cases = {}
    for kind in ("delayed_health", "initial_health", "missing_reply", "late_reply"):
        inputs = _copy(_ascent_inputs(kind))
        case_run_id = run_id+"-"+kind
        cases[kind] = {"run_id": case_run_id, "inputs": inputs,
                       "output": _run_ascent_trace(kind, case_run_id, inputs)}
    return {"schema": "missionos.starship_m3_ascent_runtime_evidence.v1",
        "source_sha256": _copy(source_sha256), "synthetic_sensor_trace": True,
        "real_model_calls": 0, "physics_steps": 0, "physical_execution": False, "cases": cases}


def verify_ascent_runtime(record, *, expected_sources):
    from .starship_replanning import digest
    _require(record["schema"] == "missionos.starship_m3_ascent_runtime_evidence.v1"
             and record["source_sha256"] == expected_sources and record["synthetic_sensor_trace"] is True
             and type(record["real_model_calls"]) is int and record["real_model_calls"] == 0
             and type(record["physics_steps"]) is int and record["physics_steps"] == 0
             and record["physical_execution"] is False
             and set(record["cases"]) == {"delayed_health", "initial_health", "missing_reply", "late_reply"}, "ascent_runtime_binding")
    for kind, case in record["cases"].items():
        _require(digest(case["inputs"]) == digest(_ascent_inputs(kind)), "ascent_runtime_fixed_inputs")
        replay = _run_ascent_trace(kind, case["run_id"], case["inputs"])
        _require(digest(case["output"]) == digest(replay), "ascent_runtime_replay_mismatch")
        actor = replay["record"]
        _require(actor["pending"] is None and actor["requests"] <= 4
                 and all(row["physical_engine_command"] is False and row["accepted_action"] is None
                         and row["fallback"] is True and row["later_observation"] is not None
                         for row in actor["decisions"]), "ascent_runtime_bounded_fallback")
        if kind in {"initial_health", "delayed_health"}:
            _require(actor["withheld"] is True and bool(actor["confirmations"])
                     and actor["requests"] == (2 if kind == "delayed_health" else 1)
                     and any(row["request"]["trigger"] == ("health_changed" if kind == "delayed_health" else "initial_monitor")
                             for row in actor["decisions"]), "ascent_runtime_health_not_supervised")
        else:
            _require(actor["withheld"] is False and actor["requests"] == 1
                     and all(row["action"] == "continue_mission" for row in actor["decisions"]), "ascent_runtime_normal_damaged")
    return {"late_or_missing": True, "ascent_health_change": True}


def verify_completion(protocol, flight_studies, runtime_records, *, expected_protocol_sha256,
                      gateway_records, host_signing_key):
    """Recheck all raw flights and registered boundary artifacts; never infer gaps."""
    import json
    from .starship_replanning import digest
    from .starship_m3_live import verify as verify_flight
    result = {"schema": "missionos.starship_m3_completion_verification.v1", "passed": False,
        "m3_completed": False, "issues": [], "flight_verification": {}, "runtime_roles": {},
        "live_phase_coverage": dict.fromkeys(PHASES, False), "physical_execution": False,
        "scope": "declared_four_flight_matrix_plus_current_source_boundary_regressions",
        "actual_run_ids": {},
        "physical_fault_robustness_qualified": False, "live_human_identity_verified": False}
    try:
        _require(digest(protocol) == expected_protocol_sha256, "m3_matrix_approval_hash")
        canonical = build_protocol(protocol["flight_bindings"], source_sha256=protocol["source_sha256"],
                                   return_source_sha256=protocol["return_source_sha256"],
                                   matrix=protocol.get("flight_matrix", "original_v1"))
        _require(digest(protocol) == digest(canonical), "m3_matrix_protocol_modified")
        policies = {b["authority"].get("prefix_authority", {}).get("prefix_provider_policy")
                    for b in protocol["flight_bindings"].values()}
        _require(len(policies) == 1, "m3_matrix_mixed_booster_provider_scope")
        if policies == {"jev_ascent_deepseek_booster_report_v1"}:
            result["live_phase_coverage"]["booster_deepseek"] = result["live_phase_coverage"].pop("booster_jev")
        _require(type(flight_studies) is dict and set(flight_studies) == set(FLIGHT_CASES), "m3_matrix_missing_flight")
        _require(type(runtime_records) is dict and set(runtime_records) == {"scope", "ascent"}, "m3_matrix_missing_runtime")
        _require(type(gateway_records) is dict and set(gateway_records) == set(FLIGHT_CASES), "m3_gateway_bindings_missing")
        for role, binding in protocol["flight_bindings"].items():
            raw = flight_studies[role]
            _require(type(raw) is bytes, "m3_raw_study_bytes_required")
            study = json.loads(raw)
            run_id = _gateway_binding(gateway_records[role], raw, binding,
                expected_sources=protocol["source_sha256"], signing_key=(
                    host_signing_key.get(role) if type(host_signing_key) is dict else host_signing_key))
            _require(run_id not in result["actual_run_ids"].values(), "m3_matrix_unique_flights")
            result["actual_run_ids"][role] = run_id
            extra = gateway_records[role].get("inflight_verification_inputs", {})
            _require(type(extra) is dict and set(extra) <= {"expected_inflight_binding", "expected_host_evidence"},
                     "m3_registered_inflight_verification_inputs")
            verdict = verify_flight(study, expected_authority=binding["authority"],
                expected_sources=protocol["source_sha256"], expected_return_sources=protocol["return_source_sha256"],
                expected_run_id=run_id, **extra)
            result["flight_verification"][role] = verdict
            _require(verdict["passed"] is True and verdict["case_accepted"] is True, "m3_matrix_flight_failed:"+role)
            _require(study["authority"]["mode"] == "live", "m3_matrix_fixture_as_live")
            coverage = phase_coverage(study)
            for key, value in coverage.items():
                result["live_phase_coverage"][key] |= value
            inner = study["m2_study"]
            if role == "normal":
                handoff = inner["handoff"]
                commands = [row["action"] for row in handoff["history"] if row["kind"] == "command"]
                _require(handoff["released_count"] == handoff["total_count"]
                         and all(action == "continue" for action in commands)
                         and inner["return_study"]["dispatch"]["candidate_id"] == "nominal", "m3_normal_unnecessary_intervention")
            elif role == "deployment_change":
                handoff = inner["handoff"]
                _require(0 <= handoff["released_count"] < handoff["total_count"]
                         and any(_adopted([decision], "jev_invocation", "typesafe")
                                 and decision["response"]["action"] == "stop_deployment"
                                 for decision in inner["deployment_run"]["mission_director"]["records"]),
                         "m3_changed_inventory_or_live_decision_missing")
            elif role == "scope_amendment":
                amended = verdict.get("inflight_verification") or {}
                _require(amended.get("passed") is True and amended.get("scope_amendment_applied") is True
                         and amended.get("reservation_observed") is True,
                         "m3_live_scope_amendment_unproven")
                result["scope_http_verification"] = verify_live_operator_http(
                    gateway_records[role]["operator_http_record"],
                    host=extra["expected_host_evidence"], run_id=run_id)
                _require(any(_adopted([decision], "jev_invocation", "typesafe")
                             or _adopted([decision], "llm_invocation", "deepseek")
                             for decision in inner["return_study"]["decisions"]
                             if decision.get("accepted_action") == "select_next_orbit"),
                         "m3_scope_request_not_live_ai")
            elif role == "ambiguous_comparison":
                from .starship_reference_ids import verify_reference_ids
                audits = []
                for decision in inner["return_study"]["decisions"]:
                    if (not _adopted([decision], "llm_invocation", "deepseek")
                            or decision["accepted_action"] not in {"keep_plan", "select_nominal", "select_next_orbit"}):
                        continue
                    basis = decision["response"].get("decision_basis") or {}
                    audits.append(verify_reference_ids(decision["request"], basis.get("reference_ids"),
                                                       basis.get("compared_candidate_ids")))
                _require(coverage["return_deepseek"] and any(audit["status"] == "valid"
                         and len(audit["referenced_candidate_ids"]) >= 2 for audit in audits),
                         "m3_live_comparison_reference_evidence_missing")
                result["comparison_reference_audits"] = audits
        result["runtime_roles"].update(verify_scope_runtime(runtime_records["scope"], expected_sources=protocol["source_sha256"]))
        result["runtime_roles"].update(verify_ascent_runtime(runtime_records["ascent"], expected_sources=protocol["source_sha256"]))
        _require(set(result["runtime_roles"]) == set(RUNTIME_ROLES) and all(result["runtime_roles"].values()), "m3_runtime_matrix_incomplete")
        _require(all(result["live_phase_coverage"].values()), "m3_live_provider_coverage_incomplete")
        result.update(passed=True, m3_completed=True)
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
        result["issues"].append(str(error) if isinstance(error, ValueError) else "malformed_m3_matrix:"+type(error).__name__)
    return result


def _gateway_binding(record, raw_study, binding, *, expected_sources, signing_key):
    """Authenticate locally assigned run identity without preregistering UUIDs.

    The local key is supplied in memory by the owning campaign, never emitted in
    a report. This authenticates local runtime receipts, not human SSO identity.
    """
    import hashlib
    import hmac
    from .starship_replanning import digest
    _require(type(signing_key) is bytes and len(signing_key) == 32, "m3_gateway_signing_key_required")
    plan, approval, receipt, execution = (record[k] for k in ("plan", "approval", "receipt", "execution"))
    additional = record.get("inflight_verification_inputs", {})
    host = additional.get("expected_host_evidence")
    signed_rows = [approval, receipt]
    if host is not None:
        signed_rows.append(host)
    for signed in signed_rows:
        expected = hmac.new(signing_key, digest({k: v for k, v in signed.items()
                                               if k != "signature"}).encode(), "sha256").hexdigest()
        _require(type(signed.get("signature")) is str and hmac.compare_digest(signed["signature"], expected),
                 "m3_gateway_receipt_signature")
    _require(plan["sha256"] == digest({k: v for k, v in plan.items() if k != "sha256"})
             and plan["source_sha256"] == expected_sources and plan["scenario"] == binding["scenario"]
             and plan["mission_envelope"] == binding["authority"]
             and approval["mission_envelope"] == binding["authority"]
             and approval["plan_sha256"] == receipt["plan_sha256"] == plan["sha256"]
             and approval["plan_id"] == plan["id"] and approval["session_id"] == plan["session_id"]
             and approval["operator_command_received"] is True
             and approval["physical_execution_authorized"] is False
             and receipt["physical_execution"] is False
             and receipt["schema"] == "missionos.starship_worker_receipt.v1"
             and receipt["verification_passed"] is True
             and receipt["artifact_sha256"]["study.json"] == hashlib.sha256(raw_study).hexdigest()
             and execution["worker_pid"] == receipt["worker_pid"]
             and approval["created_at_epoch_s"] <= execution["started_at_epoch_s"] < approval["expires_at_epoch_s"]
             and execution["run_id"] == receipt["run_id"] == approval["consumed_by_run"],
             "m3_gateway_plan_run_artifact_binding")
    return receipt["run_id"]


def verify_live_operator_http(record, *, host, run_id):
    """Bind observed HTTP messages to the authenticated host's exact grant.

    A scripted authenticated principal is not a verified human. The actor's
    reservation and physical effects are checked by the separate flight verifier.
    """
    ledger = host["signed_ledger_record"]
    request, receipt = ledger["request"], ledger["receipt"]
    _require(record["schema"] == "missionos.starship_m3_scripted_operator_http.v1"
             and record["run_id"] == run_id and record["choice"] == "approve"
             and record["resolved"] is True and record["physical_execution"] is False
             and record["operator_response_source"] == host["operator_response_source"] == "scripted_test_driver"
             and record["authenticated_human_identity_verified"] is False
             and host["host_ledger_signatures_verified"] is True
             and receipt["choice"] == "approve" and receipt["local_operator_authenticated"] is True,
             "m3_operator_http_identity")
    base = "/missionos/starship/inflight/runs/"+run_id+"-return/missions/"+run_id+"/vehicles/ship"
    path = base+"/requests/"+request["request_id"]+"/approve"
    expected_body = {"request_sha256": request["sha256"],
                     "observed_state_sha256": request["observed_state_sha256"],
                     "candidate_sha256": request["candidate_sha256"]}
    pending = [i for i, row in enumerate(record["http"]) if row["path"] == base+"/pending"
               and row["method"] == "GET" and row["request_body"] is None and row["status"] == 200
               and row["response_body"].get("request") == request
               and row["response_body"].get("resolvable") is True]
    approved = [i for i, row in enumerate(record["http"]) if row["path"] == path and row["method"] == "POST"
                and row["request_body"] == expected_body and row["status"] == 200
                and row["response_body"].get("receipt") == receipt
                and row["response_body"].get("executor_command_issued") is False]
    replay = [i for i, row in enumerate(record["http"]) if row["path"] == path and row["method"] == "POST"
              and row["request_body"] == expected_body and row["status"] == 409]
    _require(bool(pending) and len(approved) == 1 and bool(replay)
             and min(pending) < approved[0] < max(replay), "m3_operator_http_delivery_approval_replay")
    return {"passed": True, "http_delivered": True, "local_operator_authenticated": True,
            "replay_rejected": True, "operator_response_source": "scripted_test_driver",
            "human_operator_action_verified": False, "sso_identity_verified": False}

"""Independent return-context reconstruction; invented contacts, never flights."""

from copy import deepcopy

import pytest

from src.runtime import starship_restored_native_verifier as native
from src.runtime import starship_restored_return_context as producer
from src.runtime import starship_restored_decision_pipe as pipe
from tests.contract import test_starship_restored_native_verifier as prior

saved_only = prior.saved_only


def context_case(monkeypatch, *, mutate=None, marker=True, rejected=False):
    """Real saved orchestration/artifacts; prefix/contact/provider data synthetic."""
    original_case = prior.case

    def source_case(*args, **kwargs):
        values = list(original_case(*args, **kwargs))
        values[4] = {**values[4], "src/runtime/starship_restored_return_context.py": "d" * 64}
        return tuple(values)

    monkeypatch.setattr(prior, "case", source_case)
    from src.runtime.starship_restored_coordinator import RestoredReturnCoordinator

    # This old fixture intentionally builds its object without __init__.
    monkeypatch.setattr(
        RestoredReturnCoordinator, "return_context_projection", False, raising=False
    )
    record, profile, args = prior.synthetic_native_closed(
        monkeypatch, live=True, reject_next=rejected
    )
    record = native._read(native._wire(record))
    host = native._read(args["host_record_raw"])
    original = native._origin(
        record["governance"]["closed_origin"]["raw"],
        args["artifacts"],
        args["artifacts"][native.contract.PROFILE_SHA256],
        args["expected_source_sha256"],
    )
    forecasts = native._forecast_batches(
        record["governance"]["forecast_batches"],
        args["artifacts"],
        args["artifacts"][native.contract.PROFILE_SHA256],
        args["expected_source_sha256"],
        args["expected_integration_policy"],
        original,
        host,
    )
    decision = record["governance"]["decisions"][0]
    pairs = {}
    for identity, row in decision["request_candidates"].items():
        candidate, notice, resources = native._candidate(
            row,
            forecasts,
            args["artifacts"][native.contract.PROFILE_SHA256],
            args["expected_source_sha256"],
            original,
        )
        check = native._saved_admission(
            candidate, decision["request_snapshot"], original, notice, resources, "selection"
        )
        pairs[identity] = candidate, check
    old = deepcopy(
        next(
            row["frame"]["request"]
            for row in host["decision_host"]["requests"]
            if row["frame"]["request"]["phase"] == "return"
        )
    )
    new = deepcopy(old)
    if marker:
        new["facts"].update(
            producer.projection_facts(
                decision["request_snapshot"]["binding"],
                {key: (candidate.record(), check) for key, (candidate, check) in pairs.items()},
                observed_s=old["time_s"],
            )
        )
    if mutate is not None:
        mutate(new)
    from src.intelligence.starship_restored_agent import public_input

    substitutions = {
        native._hash(old): native._hash(new),
        native._hash(public_input(old, provider="typesafe")): native._hash(
            public_input(new, provider="typesafe")
        ),
    }

    def rewrite(value):
        if type(value) is dict:
            if value == old:
                return deepcopy(new)
            return {key: rewrite(item) for key, item in value.items()}
        if type(value) is list:
            return [rewrite(item) for item in value]
        return substitutions.get(value, value) if type(value) is str else value

    record, host = rewrite(record), rewrite(host)
    args.update(
        host_record_raw=native._wire(host),
        expected_host_record_sha256=native._hash(host),
        expected_return_context_projection=True,
    )
    return record, profile, args, new, pairs


def forbid_projection(monkeypatch):
    def forbidden(*_a, **_kw):
        pytest.fail("Saved checker reused return-context producer")

    for name in ("projection_facts", "validate_context", "public_projection"):
        monkeypatch.setattr(producer, name, forbidden)
    from src.intelligence import starship_restored_agent as agent

    for name in ("validate_context", "public_projection"):
        if hasattr(agent, name):
            monkeypatch.setattr(agent, name, forbidden)
    from src.runtime import starship_restored_coordinator as coordinator

    monkeypatch.setattr(coordinator, "projection_facts", forbidden)


def test_real_projection_roundtrip_then_all_context_producers_forbidden(monkeypatch):
    record, profile, args, request, pairs = context_case(monkeypatch)
    from src.intelligence.starship_restored_agent import public_input

    expected_public = public_input(request, provider="typesafe")
    expected_facts = {
        key: value for key, value in request["facts"].items() if native._context_fact_key(key)
    }
    snapshot = record["governance"]["decisions"][0]["request_snapshot"]
    forbid_projection(monkeypatch)
    assert native._return_context_facts(snapshot, pairs) == expected_facts
    assert native._return_context_public_input(request, "typesafe") == expected_public
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["physical_success"] is result["ai_control_completed"] is True
    assert result["hosted_model_attempts"] == result["confirmed_native_inferences"] == 3
    assert result["reservation_change_count"] == 0 and result["return_candidate_id"] == "nominal"


def test_contrary_valid_choice_remains_valid_when_independent_tools_reject_selected_candidate(
    monkeypatch,
):
    record, profile, args, request, _ = context_case(monkeypatch, rejected=True)
    assert request["facts"]["candidate.next_orbit.feasibility_passed"] is False
    assert request["facts"]["candidate.next_orbit.admission_reason_count"] > 0
    assert (
        request["facts"][
            "candidate.next_orbit.reason.synthetic_area_or_contact_availability_window"
        ]
        is True
    )
    forbid_projection(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["judgment_chain_verified"] is result["physical_success"] is True
    assert result["honored_native_return_choice"] is result["ai_control_completed"] is False
    assert (
        record["governance"]["decisions"][0]["disposition"]
        == "valid_choice_rejected_not_overwritten"
    )


def test_coherently_rehashed_reservation_cannot_replace_actual_snapshot(monkeypatch):
    def changed(req):
        req["facts"].update(
            {
                "reservation.nominal": False,
                "reservation.next_orbit": True,
                "reservation.return_time_s": req["facts"]["candidate.next_orbit.return_time_s"],
            }
        )

    record, profile, args, _, _ = context_case(monkeypatch, mutate=changed)
    forbid_projection(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and "decision_time_numeric_tools" in str(result["issues"])


def test_coherently_rehashed_unavailable_flag_cannot_replace_registered_operational_sites(
    monkeypatch,
):
    def changed(req):
        req["facts"].update(
            {
                "notice.nominal_available": False,
                "candidate.nominal.feasibility_passed": False,
                "candidate.nominal.admission_reason_count": 1,
                "candidate.nominal.reason.synthetic_area_or_contact_availability_window": True,
            }
        )

    record, profile, args, _, _ = context_case(monkeypatch, mutate=changed)
    forbid_projection(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and "decision_time_numeric_tools" in str(result["issues"])


@pytest.mark.parametrize("expected,marker", [(True, False), (False, True)])
def test_expected_optin_requires_every_marker_and_refuses_undeclared_projection(
    monkeypatch, expected, marker
):
    record, profile, args, _, _ = context_case(monkeypatch, marker=marker)
    args["expected_return_context_projection"] = expected
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and "opt_in_dropped_or_undeclared" in str(result["issues"])


@pytest.mark.parametrize("flag", [None, 0, 1, "true"])
def test_optin_is_explicit_bool_not_numeric_or_truthy(monkeypatch, flag):
    record, profile, args, _, _ = context_case(monkeypatch)
    args["expected_return_context_projection"] = flag
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and "must_be_bool" in str(result["issues"])


@pytest.mark.parametrize(
    "change",
    [
        "version_bool",
        "version_zero",
        "exclusive",
        "time",
        "available_integer",
        "capability",
        "count_bool",
        "count_missing_reason",
        "false_reason",
        "unknown_reason",
        "accepted",
        "authority_integer",
        "extra_reservation",
    ],
)
def test_strict_independent_wire_projection_types_sparse_reasons_and_current_reservation(
    monkeypatch, change
):
    _, _, _, request, _ = context_case(monkeypatch)
    facts = request["facts"]
    if change == "version_bool":
        facts["return_context.version"] = True
    elif change == "version_zero":
        facts["return_context.version"] = 0
    elif change == "exclusive":
        facts["reservation.next_orbit"] = True
    elif change == "time":
        facts["reservation.return_time_s"] += 1.0
    elif change == "available_integer":
        facts["notice.nominal_available"] = 1
    elif change == "capability":
        facts["candidate.nominal.numerical_contact_capability_met"] = False
    elif change == "count_bool":
        facts["candidate.nominal.admission_reason_count"] = False
    elif change == "count_missing_reason":
        facts["candidate.nominal.admission_reason_count"] = 1
    elif change == "false_reason":
        facts["candidate.nominal.reason.notice_stale"] = False
    elif change == "unknown_reason":
        facts["candidate.nominal.reason.model_is_certain"] = True
    elif change == "accepted":
        facts["candidate.nominal.feasibility_passed"] = False
    elif change == "authority_integer":
        facts["authority.next_orbit_allowed"] = 0
    else:
        facts["reservation.source_is_authenticated"] = True
    forbid_projection(monkeypatch)
    with pytest.raises(ValueError):
        native._context_request(request)


def test_reservation_switch_to_next_uses_actual_binding_and_no_new_time_invention(monkeypatch):
    record, _, _, request, pairs = context_case(monkeypatch)
    snapshot = deepcopy(record["governance"]["decisions"][0]["request_snapshot"])
    snapshot["binding"]["candidate_id"] = "next_orbit"
    snapshot["binding"]["return_time_s"] = request["facts"]["candidate.next_orbit.return_time_s"]
    expected = producer.projection_facts(
        snapshot["binding"],
        {key: (c.record(), check) for key, (c, check) in pairs.items()},
        observed_s=snapshot["state"]["time_s"],
    )
    forbid_projection(monkeypatch)
    assert native._return_context_facts(snapshot, pairs) == expected


def judgment(request, provider, action):
    return {
        "schema": "missionos.starship_restored_judgment.v1",
        "request_id": request["request_id"],
        "request_sha256": native._hash(request),
        "provider": provider,
        "status": "valid",
        "action": action,
        "execution_permission_granted": False,
        "model_inference_invoked": False,
        "provider_receipt": {},
    }


def exchange(request, deep=False):
    first = judgment(
        request, "typesafe", "request_deep_reasoning" if deep else "keep_reserved_return"
    )
    rows = [
        {
            "provider": "typesafe",
            "request_sha256": native._hash(request),
            "decision_deadline_s": request["deadline_s"],
            "remaining_s": 180.0,
            "judgment": first,
        }
    ]
    final = first
    if deep:
        selected = deepcopy(request)
        selected["allowed_actions"].remove("request_deep_reasoning")
        selected["deadline_s"] = selected["time_s"] + 150.0
        final = judgment(selected, "deepseek", "keep_reserved_return")
        rows.append(
            {
                "provider": "deepseek",
                "request_sha256": native._hash(selected),
                "decision_deadline_s": selected["deadline_s"],
                "remaining_s": 150.0,
                "judgment": final,
            }
        )
    return pipe._result(request, rows, final)


@pytest.mark.parametrize("deep", [False, True])
def test_original_and_explicit_deep_exchange_deadlines_claims_preserved_without_producer(
    monkeypatch, deep
):
    _, _, _, request, _ = context_case(monkeypatch)
    result = exchange(request, deep)
    before = deepcopy(result)
    forbid_projection(monkeypatch)
    assert native._checked_exchange(result, request) == before
    assert result == before
    if deep:
        selected = native._checked_deep_request(request, 150.0)
        assert (
            selected["deadline_s"] == request["time_s"] + 150.0
            and selected["facts"] == request["facts"]
        )
        assert "request_deep_reasoning" not in selected["allowed_actions"]


@pytest.mark.parametrize(
    "where", ["top", "first", "last", "final", "action", "deadline", "delegation"]
)
def test_digest_translation_does_not_hide_original_protocol_tampering(monkeypatch, where):
    _, _, _, request, _ = context_case(monkeypatch)
    result = exchange(request, deep=True)
    if where == "top":
        result["request_sha256"] = "f" * 64
    elif where == "first":
        result["attempts"][0]["request_sha256"] = "f" * 64
    elif where == "last":
        result["attempts"][1]["judgment"]["request_sha256"] = "f" * 64
    elif where == "final":
        result["judgment"]["request_sha256"] = "f" * 64
    elif where == "action":
        result["attempts"][1]["judgment"]["action"] = "continue_deployment"
    elif where == "deadline":
        result["attempts"][1]["decision_deadline_s"] += 1.0
    else:
        result["attempts"][0]["judgment"]["action"] = "keep_reserved_return"
    forbid_projection(monkeypatch)
    with pytest.raises(ValueError):
        native._checked_exchange(result, request)


def test_legacy_request_has_identical_verdict_with_default_and_explicit_false(monkeypatch):
    record, profile, args = prior.synthetic_native_closed(monkeypatch)
    before = native.verify_restored_native_mission(record, profile, **args)
    explicit = native.verify_restored_native_mission(
        record, profile, **args, expected_return_context_projection=False
    )
    assert before["passed"] and native._wire(before) == native._wire(explicit)


def test_valid_keep_of_unavailable_current_reservation_is_preserved_not_adopted(monkeypatch):
    """Real saved candidate admissions; no host, controller or dispatch starts."""
    from dataclasses import replace

    record, _profile, args, request, pairs = context_case(monkeypatch)
    governance = record["governance"]
    original = native._origin(
        governance["closed_origin"]["raw"],
        args["artifacts"],
        args["artifacts"][native.contract.PROFILE_SHA256],
        args["expected_source_sha256"],
    )
    host = native._read(args["host_record_raw"])
    forecasts = native._forecast_batches(
        governance["forecast_batches"],
        args["artifacts"],
        args["artifacts"][native.contract.PROFILE_SHA256],
        args["expected_source_sha256"],
        args["expected_integration_policy"],
        original,
        host,
    )
    operations = deepcopy(args["expected_operations"])
    sites = tuple(
        replace(
            native.admission.RestoredSitePolicy(
                **{
                    key: row[key]
                    for key in (
                        "site_id",
                        "latitude_deg",
                        "longitude_deg",
                        "radius_m",
                        "opens_s",
                        "closes_s",
                    )
                }
            ),
            opens_s=7000.0,
            closes_s=16000.0,
        )
        for row in operations["sites"]
    )
    operations["sites"] = [site.record() for site in sites]
    decision = governance["decisions"][0]
    updated = {}
    facts = native._context_legacy(request)["facts"]
    for identity, (candidate, _old_check) in pairs.items():
        raw = candidate.record()
        notice = native.admission.RestoredOperationalNotice(
            raw["notice"]["generation"],
            raw["notice"]["issued_s"],
            raw["notice"]["expires_s"],
            tuple(raw["notice"]["available_site_ids"]),
        )
        resources = native.admission.RestoredResourceState(
            **{
                key: raw["resources"][key]
                for key in (
                    "generation",
                    "observed_s",
                    "energy_kwh",
                    "temperature_k",
                    "modeled_propellant_loss_kg",
                )
            }
        )
        rebuilt = native.admission.RestoredCandidate(
            native.RestoredReturnResult(candidate._prediction_json),
            original,
            native.contract.RestoredCandidateSchedule(original),
            args["artifacts"][native.contract.PROFILE_SHA256],
            args["expected_source_sha256"],
            native.admission.RestoredAdmissionPolicy(),
            sites,
            notice,
            resources,
        )
        check = native._saved_admission(
            rebuilt, decision["request_snapshot"], original, notice, resources, "selection"
        )
        decision["request_candidates"][identity] = rebuilt.record()
        updated[identity] = rebuilt, check
        facts[f"candidate.{identity}.feasibility_passed"] = check["accepted"]
    assert updated["nominal"][1]["accepted"] is False
    facts.update(
        producer.projection_facts(
            decision["request_snapshot"]["binding"],
            {key: (candidate.record(), check) for key, (candidate, check) in updated.items()},
            observed_s=request["time_s"],
        )
    )
    request = {**request, "facts": facts}
    decision.update(
        original_action="keep_reserved_return",
        disposition="valid_choice_rejected_not_overwritten",
        candidate=updated["nominal"][0].record(),
        admission=updated["nominal"][1],
    )
    exchanges = {
        request["request_id"]: {
            "request": request,
            "exchange": {"status": "valid", "action": "keep_reserved_return"},
            "native_choice": True,
        }
    }
    forbid_projection(monkeypatch)
    used, native_chain, honored = native._return_decisions(
        governance,
        exchanges,
        original,
        forecasts,
        args["artifacts"][native.contract.PROFILE_SHA256],
        args["expected_source_sha256"],
        governance["approval"]["binding"],
        operations,
        expected_return_context_projection=True,
    )
    assert used == {request["request_id"]} and native_chain is True and honored is False
    assert decision["original_action"] == "keep_reserved_return"
    assert decision["disposition"] == "valid_choice_rejected_not_overwritten"


def test_optin_cannot_borrow_a_source_manifest_missing_the_new_context_boundary(monkeypatch):
    record, profile, args, _, _ = context_case(monkeypatch)
    args["expected_source_sha256"] = {
        key: value
        for key, value in args["expected_source_sha256"].items()
        if key != "src/runtime/starship_restored_return_context.py"
    }
    result = native.verify_restored_native_mission(record, profile, **args)
    assert not result["passed"] and "return_context_source_manifest_missing" in str(
        result["issues"]
    )

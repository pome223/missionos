"""Signed one-use choices are distinct from commands and observed flight effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import hmac
import json
import multiprocessing
import sqlite3
from threading import Barrier, Event

import pytest

from src.runtime.starship_operator_resolution import (
    OperatorResolutionError, OperatorResolutionLedger, SCOPE, verify_operator_resolution,
)
from src.runtime import starship_operator_resolution as resolution

KEY = b"local-test-signing-key-only-32-bytes"
CONTEXT = {"session_id": "operator-session", "plan_id": "plan-1", "plan_sha256": "a"*64,
           "run_id": "run-1", "request_id": "request-1"}


def observation(*, simulation=10., wall=1000., ready=True, checksum="b"*64):
    return {"evidence_sha256": checksum, "simulation_time_s": simulation, "wall_time_s": wall, "tower_ready": ready}


def pending(ledger=None, **kwargs):
    ledger = ledger or OperatorResolutionLedger(KEY)
    values = {"context": deepcopy(CONTEXT), "initial_observation": observation(), "issued_simulation_time_s": 10.,
              "issued_wall_time_s": 1000., "original_simulation_deadline_s": 85., "original_wall_deadline_s": 1075.,
              "preapproved_scope": SCOPE}
    values.update(kwargs)
    return ledger, ledger.create_pending(**values)


def resolve(ledger, request, **kwargs):
    latest = observation(simulation=14., wall=1004., checksum="c"*64)
    values = {"context": deepcopy(CONTEXT), "request_sha256": request["sha256"],
              "observed_evidence_sha256": latest["evidence_sha256"], "latest_observation": latest,
              "action": "continue_capture", "simulation_time_s": 15., "wall_time_s": 1005., "run_active": True}
    values.update(kwargs)
    return ledger.resolve(**values)


def verify(request, decision, **kwargs):
    values = {"signing_key": KEY, "expected_context": deepcopy(CONTEXT),
              "expected_latest_observation": deepcopy(decision["latest_observation"]), "expected_run_active": True}
    values.update(kwargs)
    return verify_operator_resolution(request, decision, **values)


def resign(value):
    result = deepcopy(value)
    unsigned = {k: v for k, v in result.items() if k != "signature"}
    result["signature"] = hmac.new(KEY, json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode(), "sha256").hexdigest()
    return result


def test_later_fresh_evidence_binds_original_request_without_resetting_deadline():
    ledger, request = pending()
    original = deepcopy(request)
    decision = resolve(ledger, request)
    assert decision["effective_action"] == "continue_capture"
    assert decision["request_observed_evidence_sha256"] == "b"*64
    assert decision["observed_evidence_sha256"] == "c"*64
    assert request == original
    assert ledger.records(CONTEXT)["request"] == original
    assert decision["operator_response_received"] is True
    assert decision["human_identity_authenticated"] is False
    assert decision["executor_command_issued"] is False
    assert decision["observed_effect_verified"] is False
    result = verify(request, decision)
    assert result["passed"]
    assert result["executor_outcome_verified"] is False
    assert result["authenticated_human_independently_verified"] is False
    assert result["ledger_completeness_verified"] is False


def test_supplied_authenticated_identity_remains_a_separate_flag():
    ledger, request = pending()
    decision = resolve(ledger, request, human_identity_authenticated=True)
    assert decision["human_identity_authenticated"] is True
    assert verify(request, decision)["passed"]
    assert verify(request, decision)["authenticated_human_independently_verified"] is False


def test_known_unavailable_forces_divert_without_promoting_operator_request():
    ledger, request = pending()
    latest = observation(simulation=14., wall=1004., ready=False, checksum="c"*64)
    decision = resolve(ledger, request, latest_observation=latest)
    assert decision["requested_action"] == "continue_capture"
    assert decision["effective_action"] == "divert"
    assert decision["reason"] == "tower_unavailable_forces_divert"
    assert verify(request, decision)["passed"]


def test_unknown_readiness_never_implicitly_grants_capture():
    ledger, request = pending()
    latest = observation(simulation=14., wall=1004., ready=None, checksum="c"*64)
    with pytest.raises(OperatorResolutionError, match="tower_readiness_unknown"):
        resolve(ledger, request, latest_observation=latest)
    assert ledger.records(CONTEXT)["decision"] is None
    decision = resolve(ledger, request, latest_observation=latest, action="divert")
    assert decision["effective_action"] == "divert"
    assert verify(request, decision)["passed"]


@pytest.mark.parametrize("field,value", [
    ("session_id", "other-session"), ("plan_id", "other-plan"), ("plan_sha256", "d"*64),
    ("run_id", "other-run"), ("request_id", "other-request"),
])
def test_other_context_cannot_consume_pending_choice(field, value):
    ledger, request = pending()
    changed = {**CONTEXT, field: value}
    with pytest.raises(OperatorResolutionError):
        resolve(ledger, request, context=changed)
    assert ledger.records(CONTEXT)["decision"] is None
    assert resolve(ledger, request)["effective_action"] == "continue_capture"


@pytest.mark.parametrize("change", [
    {"request_sha256": "d"*64}, {"observed_evidence_sha256": "b"*64}, {"action": "set_thrust"},
    {"action": {"gimbal": 0.}}, {"run_active": False}, {"run_active": 1},
    {"human_identity_authenticated": 1}, {"simulation_time_s": 9.}, {"wall_time_s": 999.},
    {"simulation_time_s": float("nan")}, {"wall_time_s": float("inf")},
])
def test_invalid_or_inactive_resolution_does_not_spend_slot(change):
    ledger, request = pending()
    with pytest.raises(OperatorResolutionError):
        resolve(ledger, request, **change)
    assert ledger.records(CONTEXT)["decision"] is None


@pytest.mark.parametrize("latest", [
    observation(simulation=12., wall=1004., checksum="c"*64),
    observation(simulation=14., wall=1002., checksum="c"*64),
    observation(simulation=16., wall=1004., checksum="c"*64),
    observation(simulation=14., wall=1006., checksum="c"*64),
])
def test_old_or_future_observations_are_rejected(latest):
    ledger, request = pending()
    with pytest.raises(OperatorResolutionError):
        resolve(ledger, request, latest_observation=latest)
    assert ledger.records(CONTEXT)["decision"] is None


def test_freshness_boundary_is_inclusive_but_deadline_is_exclusive():
    ledger, request = pending()
    latest = observation(simulation=13., wall=1003., checksum="c"*64)
    assert resolve(ledger, request, latest_observation=latest)["effective_action"] == "continue_capture"
    for sim, wall in ((85., 1005.), (15., 1075.)):
        ledger, request = pending()
        with pytest.raises(OperatorResolutionError, match="resolution_deadline_expired"):
            resolve(ledger, request, simulation_time_s=sim, wall_time_s=wall)


@pytest.mark.parametrize("sim,wall", [(85., 1005.), (15., 1075.), (90., 1080.)])
def test_expiry_fallback_is_divert_not_human_approval_or_execution(sim, wall):
    ledger, request = pending()
    decision = ledger.expire(context=CONTEXT, request_sha256=request["sha256"], simulation_time_s=sim,
                             wall_time_s=wall, run_active=True)
    assert decision["effective_action"] == "divert"
    assert decision["source"] == "timeout_fallback"
    assert decision["requested_action"] is None
    assert decision["operator_response_received"] is False
    assert decision["human_identity_authenticated"] is False
    assert verify(request, decision)["passed"]
    with pytest.raises(OperatorResolutionError, match="resolution_already_consumed"):
        resolve(ledger, request)


def test_fallback_cannot_be_forced_early_or_after_run_stops():
    ledger, request = pending()
    with pytest.raises(OperatorResolutionError, match="resolution_deadline_not_expired"):
        ledger.expire(context=CONTEXT, request_sha256=request["sha256"], simulation_time_s=15., wall_time_s=1005., run_active=True)
    with pytest.raises(OperatorResolutionError, match="resolution_run_not_active"):
        ledger.expire(context=CONTEXT, request_sha256=request["sha256"], simulation_time_s=85., wall_time_s=1075., run_active=False)
    assert ledger.records(CONTEXT)["decision"] is None


def test_concurrent_operator_responses_have_exactly_one_winner():
    ledger, request = pending()
    barrier = Barrier(8)
    def attempt(index):
        barrier.wait()
        try:
            return resolve(ledger, request, action="continue_capture" if index % 2 else "divert")
        except OperatorResolutionError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1
    assert results.count("resolution_already_consumed") == 7
    assert ledger.records(CONTEXT)["decision"] == winners[0]
    assert verify(request, winners[0])["passed"]


@pytest.mark.parametrize("expiry", [False, True])
def test_caller_context_mutation_during_decision_cannot_reopen_request(monkeypatch, expiry):
    ledger, request = pending()
    context = deepcopy(CONTEXT)
    latest = observation(simulation=14., wall=1004., checksum="c"*64)
    entered, release = Event(), Event()
    original = resolution._decision
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(resolution, "_decision", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if expiry:
            future = pool.submit(ledger.expire, context=context, request_sha256=request["sha256"],
                                 simulation_time_s=85., wall_time_s=1075., run_active=True, latest_observation=latest)
        else:
            future = pool.submit(resolve, ledger, request, context=context, latest_observation=latest)
        try:
            assert entered.wait(3)
            # Only caller-owned input is changed; retained ledger data is not
            # touched. The vulnerable implementation indexed by these values.
            context.update(request_id="other-request", run_id="other-run", session_id="other-session")
            latest.update(tower_ready=False, evidence_sha256="f"*64)
        finally:
            release.set()
        decision = future.result(timeout=3)
    assert decision["context"] == CONTEXT
    assert decision["latest_observation"]["evidence_sha256"] == "c"*64
    assert decision["latest_observation"]["tower_ready"] is True
    assert ledger.records(CONTEXT)["decision"] == decision
    with pytest.raises(OperatorResolutionError, match="resolution_already_consumed"):
        resolve(ledger, request, action="divert")
    assert verify(request, decision)["passed"]


def test_caller_context_mutation_during_creation_cannot_change_request_indexes(monkeypatch):
    ledger = OperatorResolutionLedger(KEY)
    context = deepcopy(CONTEXT)
    entered, release = Event(), Event()
    original = resolution._request
    def delayed(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(resolution, "_request", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pending, ledger, context=context)
        try:
            assert entered.wait(3)
            context.update(request_id="other-request", run_id="other-run")
        finally:
            release.set()
        _, request = future.result(timeout=3)
    assert request["context"] == CONTEXT
    assert ledger.records(CONTEXT)["request"] == request
    with pytest.raises(OperatorResolutionError, match="resolution_run_budget_exhausted"):
        pending(ledger, context={**CONTEXT, "request_id": "new-request"})


def test_request_and_result_copies_cannot_modify_retained_authority():
    ledger, request = pending()
    checksum = request["sha256"]
    request["allowed_actions"].append("set_thrust")
    request["original_simulation_deadline_s"] = 10000.
    decision = resolve(ledger, {"sha256": checksum})
    decision["effective_action"] = "set_thrust"
    records = ledger.records(CONTEXT)
    assert records["request"]["allowed_actions"] == ["continue_capture", "divert"]
    assert records["request"]["original_simulation_deadline_s"] == 85.
    assert records["decision"]["effective_action"] == "continue_capture"


def test_budget_does_not_reset_by_new_request_id_or_expiration():
    ledger, _ = pending()
    with pytest.raises(OperatorResolutionError, match="resolution_request_already_created"):
        pending(ledger)
    with pytest.raises(OperatorResolutionError, match="resolution_run_budget_exhausted"):
        pending(ledger, context={**CONTEXT, "request_id": "new-request"})


@pytest.mark.parametrize("kwargs", [
    {"preapproved_scope": "physical_execution"}, {"allowed_actions": ["continue_capture"]},
    {"allowed_actions": ["divert", "set_thrust"]}, {"allowed_actions": ["divert", "divert"]},
    {"original_simulation_deadline_s": 10.}, {"original_wall_deadline_s": 1000.},
    {"original_simulation_deadline_s": 1300.}, {"maximum_observation_age_s": 0.},
    {"initial_observation": observation(ready=1)}, {"initial_observation": {**observation(), "prediction": "future"}},
    {"context": {**CONTEXT, "gimbal": 0.}},
])
def test_pending_schema_scope_and_deadlines_are_closed(kwargs):
    with pytest.raises(OperatorResolutionError):
        pending(**kwargs)


@pytest.mark.parametrize("mutation", [
    lambda r, d: r.update(original_simulation_deadline_s=1200.),
    lambda r, d: r["allowed_actions"].append("set_thrust"),
    lambda r, d: d.update(effective_action="divert"),
    lambda r, d: d.update(executor_command_issued=True),
    lambda r, d: d.update(physical_execution=True),
    lambda r, d: d.update(limits_waived=True),
    lambda r, d: d.update(simulation_time_s=86.),
    lambda r, d: d["latest_observation"].update(evidence_sha256="f"*64),
])
def test_independent_verifier_rejects_changed_request_action_evidence_or_claim(mutation):
    ledger, request = pending()
    decision = resolve(ledger, request)
    authoritative = deepcopy(decision["latest_observation"])
    mutation(request, decision)
    assert not verify(request, decision, expected_latest_observation=authoritative)["passed"]


def test_independent_verifier_rejects_signed_but_wrong_rules_and_flag_mutation():
    ledger, request = pending()
    latest = observation(simulation=14., wall=1004., ready=False, checksum="c"*64)
    valid = resolve(ledger, request, latest_observation=latest)
    invalid = resign({**valid, "effective_action": "continue_capture", "reason": "fresh_tower_ready"})
    assert not verify(request, invalid)["passed"]
    invalid = resign({**valid, "operator_response_received": 1})
    assert not verify(request, invalid)["passed"]
    invalid = resign({**valid, "physical_execution": True})
    assert not verify(request, invalid)["passed"]


def test_external_context_and_latest_evidence_are_required_not_self_authenticated():
    ledger, request = pending()
    decision = resolve(ledger, request)
    assert not verify(request, decision, signing_key=b"different-local-test-key-32-bytes")["passed"]
    assert not verify(request, decision, expected_context={**CONTEXT, "run_id": "other"})["passed"]
    assert not verify(request, decision, expected_latest_observation=observation())["passed"]
    assert not verify(request, decision, expected_run_active=False)["passed"]


@pytest.mark.parametrize("key", [None, "text-key", b"short", b"a"*129])
def test_signing_key_must_be_explicit_bounded_bytes(key):
    with pytest.raises(OperatorResolutionError, match="invalid_resolution_signing_key"):
        OperatorResolutionLedger(key)


def test_request_hash_is_canonical_and_no_source_or_secret_is_in_receipt():
    _, request = pending()
    unsigned = {k: v for k, v in request.items() if k not in {"sha256", "signature"}}
    assert request["sha256"] == sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                                 allow_nan=False).encode()).hexdigest()
    assert KEY.decode() not in json.dumps(request)


def test_verifier_does_not_invoke_callback_or_custom_equality_data():
    class Untrusted:
        def __eq__(self, _):
            pytest.fail("Untrusted transaction data invoked a callback")
    ledger, request = pending()
    decision = resolve(ledger, request)
    for field in ("context", "source", "scope", "effective_action", "latest_observation", "request_sha256"):
        changed = {**decision, field: Untrusted()}
        assert not verify(request, changed)["passed"]
    changed = {**request, "scope": Untrusted()}
    assert not verify(changed, decision)["passed"]


def _process_resolution_attempt(store_path, request, ready, start, results, *, expiry=False):
    """Spawned processes receive the test key explicitly, never from storage."""
    try:
        ledger = OperatorResolutionLedger(KEY, store_path=store_path)
        ready.put(True)
        if not start.wait(10):
            results.put("test_start_timeout")
            return
        if expiry:
            result = ledger.expire(context=CONTEXT, request_sha256=request["sha256"],
                simulation_time_s=85., wall_time_s=1075., run_active=True)
        else:
            result = resolve(ledger, request)
        results.put(result)
    except OperatorResolutionError as error:
        results.put(str(error))


def _process_expiry_boundary(store_path, request, ready, start, results, *, kind):
    try:
        ledger = OperatorResolutionLedger(KEY, store_path=store_path)
        ready.put(kind)
        if not start.wait(10):
            results.put({"kind": kind, "error": "test_start_timeout"})
            return
        if kind == "human":
            decision = resolve(ledger, request)
        else:
            decision = ledger.expire_or_get(context=CONTEXT, request_sha256=request["sha256"],
                simulation_time_s=85., wall_time_s=1075., run_active=True)
        results.put({"kind": kind, "decision": decision})
    except OperatorResolutionError as error:
        results.put({"kind": kind, "error": str(error)})


@pytest.mark.parametrize("kinds", [("actor_timeout", "broker_timeout"), ("human", "actor_timeout")])
def test_sqlite_two_process_expire_or_get_returns_existing_winner_without_timeout_failure(tmp_path, kinds):
    path = tmp_path/"expiry-race.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_process_expiry_boundary,
        args=(str(path), request, ready, start, results), kwargs={"kind": kind}) for kind in kinds]
    try:
        for worker in workers:
            worker.start()
        assert {ready.get(timeout=15) for _ in workers} == set(kinds)
        start.set()
        values = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        ready.close()
        results.close()
    retained = OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)
    for value in values:
        if value["kind"] != "human":
            assert value.get("decision") == retained["decision"] and "error" not in value
        elif "decision" in value:
            assert value["decision"] == retained["decision"]
        else:
            assert value["error"] == "resolution_already_consumed"
    assert retained["request"] == request
    assert retained["decision"]["source"] in ("operator", "timeout_fallback")
    assert verify(request, retained["decision"])["passed"]
    with pytest.raises(OperatorResolutionError, match="already_consumed"):
        resolve(OperatorResolutionLedger(KEY, store_path=path), request)


@pytest.mark.parametrize("persistent", [False, True])
def test_expire_or_get_preserves_signed_operator_or_timeout_winner_and_original_deadlines(tmp_path, persistent):
    ledger = OperatorResolutionLedger(KEY, store_path=tmp_path/"operator.sqlite3" if persistent else None)
    _, request = pending(ledger)
    signed = resolve(ledger, request)
    arguments = dict(context=CONTEXT, request_sha256=request["sha256"], simulation_time_s=85., wall_time_s=1075., run_active=True)
    assert ledger.expire_or_get(**arguments) == signed
    assert ledger.expire_or_get(**{**arguments, "simulation_time_s": 86., "wall_time_s": 1076.}) == signed
    assert ledger.records(CONTEXT) == {"request": request, "decision": signed}
    with pytest.raises(OperatorResolutionError, match="not_expired"):
        ledger.expire_or_get(**{**arguments, "simulation_time_s": 15., "wall_time_s": 1005.})
    with pytest.raises(OperatorResolutionError, match="context_binding"):
        ledger.expire_or_get(**{**arguments, "context": {**CONTEXT, "session_id": "other"}})
    with pytest.raises(OperatorResolutionError, match="context_binding"):
        ledger.expire_or_get(**{**arguments, "request_sha256": "f"*64})
    other = OperatorResolutionLedger(KEY, store_path=tmp_path/"timeout.sqlite3" if persistent else None)
    _, second = pending(other)
    winner = other.expire_or_get(**{**arguments, "request_sha256": second["sha256"]})
    assert other.expire_or_get(**{**arguments, "request_sha256": second["sha256"]}) == winner
    assert winner["source"] == "timeout_fallback" and winner["operator_response_received"] is False


def test_sqlite_restart_preserves_signed_records_and_consumed_run_budget(tmp_path):
    path = tmp_path / "operator.sqlite3"
    first, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    restarted = OperatorResolutionLedger(KEY, store_path=path)
    assert restarted.records(CONTEXT) == {"request": request, "decision": None}
    decision = resolve(restarted, request)
    assert first.records(CONTEXT)["decision"] == decision
    third = OperatorResolutionLedger(KEY, store_path=path)
    assert third.records(CONTEXT)["decision"] == decision
    with pytest.raises(OperatorResolutionError, match="resolution_already_consumed"):
        resolve(third, request)
    with pytest.raises(OperatorResolutionError, match="resolution_request_already_created"):
        pending(third)
    with pytest.raises(OperatorResolutionError, match="resolution_run_budget_exhausted"):
        pending(third, context={**CONTEXT, "request_id": "another-request"})
    assert verify(request, decision)["passed"]
    assert KEY not in path.read_bytes()


@pytest.mark.parametrize("expiry", [False, True, "mixed"])
def test_sqlite_cross_process_transaction_has_one_winner_after_restart(tmp_path, expiry):
    path = tmp_path / "operator.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_process_resolution_attempt,
        args=(str(path), request, ready, start, results),
        kwargs={"expiry": bool(index % 2) if expiry == "mixed" else expiry}) for index in range(4)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=15) is True
        start.set()
        values = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        ready.close()
        results.close()
    winners = [value for value in values if isinstance(value, dict)]
    assert len(winners) == 1
    assert values.count("resolution_already_consumed") == 3
    if expiry == "mixed":
        assert winners[0]["source"] in ("timeout_fallback", "operator")
    else:
        assert winners[0]["source"] == ("timeout_fallback" if expiry else "operator")
    assert winners[0]["human_identity_authenticated"] is False
    restarted = OperatorResolutionLedger(KEY, store_path=path)
    assert restarted.records(CONTEXT)["decision"] == winners[0]
    assert verify(request, winners[0])["passed"]


def test_sqlite_concurrent_creation_has_one_request_per_run(tmp_path):
    path = tmp_path / "operator.sqlite3"
    ledgers = [OperatorResolutionLedger(KEY, store_path=path) for _ in range(6)]
    barrier = Barrier(6)
    def attempt(index):
        barrier.wait()
        try:
            return pending(ledgers[index], context={**CONTEXT, "request_id": f"request-{index}"})[1]
        except OperatorResolutionError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1
    assert results.count("resolution_run_budget_exhausted") == 5
    retained = OperatorResolutionLedger(KEY, store_path=path).records(winners[0]["context"])
    assert retained == {"request": winners[0], "decision": None}


def test_sqlite_rejected_action_or_stale_evidence_rolls_back_without_consuming(tmp_path):
    path = tmp_path / "operator.sqlite3"
    ledger, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    for changes in ({"action": "set_thrust"}, {"observed_evidence_sha256": "d"*64},
                    {"latest_observation": observation(simulation=11., wall=1004., checksum="c"*64)},
                    {"run_active": False}, {"simulation_time_s": 85.}):
        with pytest.raises(OperatorResolutionError):
            resolve(ledger, request, **changes)
        assert OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)["decision"] is None
    assert resolve(OperatorResolutionLedger(KEY, store_path=path), request)["effective_action"] == "continue_capture"


@pytest.mark.parametrize("field,value", [
    ("session_id", "other-session"), ("plan_id", "other-plan"), ("plan_sha256", "e"*64),
    ("run_id", "other-run"), ("request_id", "other-request"),
])
def test_sqlite_context_binding_survives_restart(field, value, tmp_path):
    path = tmp_path / "operator.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    restarted = OperatorResolutionLedger(KEY, store_path=path)
    with pytest.raises(OperatorResolutionError):
        resolve(restarted, request, context={**CONTEXT, field: value})
    assert restarted.records(CONTEXT)["decision"] is None
    assert resolve(restarted, request)["context"] == CONTEXT


def test_sqlite_pending_and_returned_copies_do_not_mutate_retained_context_or_deadlines(tmp_path):
    path = tmp_path / "operator.sqlite3"
    context, initial = deepcopy(CONTEXT), observation()
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path), context=context, initial_observation=initial)
    original = deepcopy(request)
    context.update(session_id="other", request_id="changed")
    initial.update(tower_ready=False, evidence_sha256="e"*64)
    request["context"].update(run_id="changed")
    request["original_simulation_deadline_s"] = 1200.
    records = OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)
    assert records["request"] == original
    records["request"]["allowed_actions"].append("set_thrust")
    ledger = OperatorResolutionLedger(KEY, store_path=path)
    decision = resolve(ledger, original)
    decision["source"] = "timeout_fallback"
    assert ledger.records(CONTEXT)["decision"]["source"] == "operator"
    assert verify(original, ledger.records(CONTEXT)["decision"])["passed"]


def test_sqlite_caller_context_mutation_during_resolution_cannot_reopen_request(tmp_path, monkeypatch):
    path = tmp_path / "operator.sqlite3"
    ledger, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    context, latest = deepcopy(CONTEXT), observation(simulation=14., wall=1004., checksum="c"*64)
    entered, release = Event(), Event()
    original = resolution._decision
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(resolution, "_decision", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resolve, ledger, request, context=context, latest_observation=latest)
        try:
            assert entered.wait(3)
            context.update(request_id="other-request", run_id="other-run", session_id="other-session")
            latest.update(evidence_sha256="f"*64, tower_ready=False)
        finally:
            release.set()
        decision = future.result(timeout=3)
    restarted = OperatorResolutionLedger(KEY, store_path=path)
    assert restarted.records(CONTEXT)["decision"] == decision
    assert decision["context"] == CONTEXT
    assert decision["latest_observation"]["evidence_sha256"] == "c"*64
    assert decision["latest_observation"]["tower_ready"] is True
    with pytest.raises(OperatorResolutionError, match="resolution_already_consumed"):
        resolve(restarted, request)


@pytest.mark.parametrize("sim,wall", [(85., 1005.), (15., 1075.)])
def test_sqlite_original_expiry_does_not_extend_on_restart_and_timeout_binds_evidence(tmp_path, sim, wall):
    path = tmp_path / "operator.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    latest = observation(simulation=14., wall=1004., ready=False, checksum="c"*64)
    restarted = OperatorResolutionLedger(KEY, store_path=path)
    with pytest.raises(OperatorResolutionError, match="resolution_deadline_expired"):
        resolve(restarted, request, simulation_time_s=sim, wall_time_s=wall, latest_observation=latest)
    decision = restarted.expire(context=CONTEXT, request_sha256=request["sha256"], simulation_time_s=sim,
                               wall_time_s=wall, run_active=True, latest_observation=latest)
    assert decision["source"] == "timeout_fallback"
    assert decision["observed_evidence_sha256"] == "c"*64
    assert decision["request_observed_evidence_sha256"] == "b"*64
    assert decision["effective_action"] == "divert"
    assert verify(request, decision)["passed"]
    assert OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)["request"] == request


@pytest.mark.parametrize("target,field,value", [
    ("request_json", "observed_evidence_sha256", "f"*64),
    ("request_json", "original_simulation_deadline_s", 1200.),
    ("decision_json", "source", "timeout_fallback"),
    ("decision_json", "observed_evidence_sha256", "f"*64),
    ("decision_json", "effective_action", "divert"),
])
def test_sqlite_altered_signed_request_source_or_evidence_is_rejected(tmp_path, target, field, value):
    path = tmp_path / "operator.sqlite3"
    ledger, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    resolve(ledger, request)
    with sqlite3.connect(path) as database:
        row = database.execute(f"SELECT {target} FROM operator_resolution_requests").fetchone()
        changed = json.loads(row[0])
        changed[field] = value
        database.execute(f"UPDATE operator_resolution_requests SET {target} = ?", (json.dumps(changed),))
    with pytest.raises(OperatorResolutionError, match="resolution_store_record_invalid"):
        OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)


def test_sqlite_row_indexes_must_match_signed_context(tmp_path):
    path = tmp_path / "operator.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    with sqlite3.connect(path) as database:
        database.execute("UPDATE operator_resolution_requests SET run_id = 'other-run'")
    ledger = OperatorResolutionLedger(KEY, store_path=path)
    with pytest.raises(OperatorResolutionError, match="resolution_store_record_invalid"):
        resolve(ledger, request)


def test_sqlite_signature_alone_does_not_bypass_source_rules_or_original_expiry(tmp_path):
    path = tmp_path / "operator.sqlite3"
    ledger, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    decision = resolve(ledger, request)
    changed = resign({**decision, "source": "timeout_fallback", "requested_action": None,
        "effective_action": "divert", "reason": "original_deadline_expired", "operator_response_received": False})
    # A valid signature still cannot turn an unexpired operator response into
    # a timeout. Persistent reads reconstruct the same independent Rules.
    with sqlite3.connect(path) as database:
        database.execute("UPDATE operator_resolution_requests SET decision_json = ?", (json.dumps(changed),))
    with pytest.raises(OperatorResolutionError, match="resolution_store_record_invalid"):
        OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)


def test_sqlite_wrong_signing_key_does_not_modify_store(tmp_path):
    path = tmp_path / "operator.sqlite3"
    _, request = pending(OperatorResolutionLedger(KEY, store_path=path))
    with pytest.raises(OperatorResolutionError, match="resolution_store_key_mismatch"):
        OperatorResolutionLedger(b"other-explicit-local-test-key-32bytes", store_path=path)
    assert OperatorResolutionLedger(KEY, store_path=path).records(CONTEXT)["request"] == request


@pytest.mark.parametrize("path", ["", "   ", ":memory:", "nul\0path", b"bytes", 17, True])
def test_sqlite_store_path_is_an_explicit_filesystem_path(path):
    with pytest.raises(OperatorResolutionError, match="invalid_resolution_store_path"):
        OperatorResolutionLedger(KEY, store_path=path)


def test_sqlite_unavailable_store_does_not_fallback_to_memory(tmp_path):
    with pytest.raises(OperatorResolutionError, match="resolution_store_unavailable"):
        OperatorResolutionLedger(KEY, store_path=tmp_path / "missing-parent" / "operator.sqlite3")

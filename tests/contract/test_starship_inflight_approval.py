"""Local authority ledger tests; no flight, HTTP, provider or human execution."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3

import pytest

from src.runtime.starship_inflight_approval import InflightApprovalError, InflightApprovalLedger, digest

KEY = b"fixture-integrity-material-not-secret"
TOKEN = "fixture-operator-bearer-token-not-secret"
REGISTRY = {"commands": ["continue", "hold", "resume"], "resource_limits": {"hold_seconds": 60., "fuel_kg": 50.}}
SCOPE = {"commands": ["continue", "hold"], "resource_limits": {"hold_seconds": 30., "fuel_kg": 10.}}
DELTA = {"add_commands": ["resume"], "resource_limits": {"hold_seconds": 45.}}


@pytest.fixture
def setup(tmp_path):
    clock = [100.]
    ledger = InflightApprovalLedger(tmp_path/"approval.sqlite", signing_key=KEY, operator_token=TOKEN,
        operator_id="operator-local", registry=REGISTRY, clock=lambda: clock[0])
    return ledger, clock


def observation(sim=10., wall=100., state="a"*64):
    return {"state_sha256": state, "simulation_time_s": sim, "wall_time_s": wall}


def create(ledger, **changes):
    args = dict(request_id="r1", mission_id="mission1", vehicle_id="ship", revision=3,
        scope=deepcopy(SCOPE), delta=deepcopy(DELTA), observation=observation(), simulation_time_s=10.,
        simulation_deadline_s=25., wall_deadline_s=115., fallback="continue")
    args.update(changes)
    return ledger.create(**args)


def resolve(ledger, request, **changes):
    args = dict(request_id="r1", request_sha256=request["sha256"], observed_state_sha256=request["observed_state_sha256"],
        choice="approve", bearer_token=TOKEN, simulation_time_s=11.)
    args.update(changes)
    return ledger.resolve(**args)


def consume(ledger, **changes):
    args = dict(request_id="r1", mission_id="mission1", vehicle_id="ship", revision=3,
        scope=deepcopy(SCOPE), observation=observation(12., 102., "b"*64), simulation_time_s=12., run_active=True)
    args.update(changes)
    return ledger.consume(**args)


def test_approve_then_fresh_observation_consumes_once(setup):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    receipt = resolve(ledger, request)
    assert receipt["local_operator_authenticated"] and not receipt["sso_identity_verified"]
    clock[0] = 102.
    result = consume(ledger)
    assert result["approved"] and result["revision"] == 4
    assert result["scope"]["commands"] == ["continue", "hold", "resume"]
    assert result["scope"]["resource_limits"]["hold_seconds"] == 45.
    assert result["scope_sha256"] == digest(result["scope"])
    assert result["latest_observation"]["state_sha256"] != request["observed_state_sha256"]
    assert result["requires_executor_check"] and not result["executor_command_issued"]
    with pytest.raises(InflightApprovalError, match="already_consumed"):
        consume(ledger)


@pytest.mark.parametrize("token", [None, "wrong", "模型"*32, 123, "x"*513])
def test_unauthenticated_model_cannot_approve(setup, token):
    ledger, _ = setup
    request = create(ledger)
    with pytest.raises(InflightApprovalError, match="authentication_failed"):
        resolve(ledger, request, bearer_token=token)
    assert ledger.status("r1")["receipt"] is None


def test_consumer_handle_has_no_approval_capability(setup):
    ledger, _ = setup
    request = create(ledger)
    consumer = InflightApprovalLedger(ledger._path, signing_key=KEY, operator_token=None,
        operator_id="operator-local", registry=REGISTRY, clock=lambda: 101.)
    with pytest.raises(InflightApprovalError, match="authentication_failed"):
        resolve(consumer, request)


def test_http_status_authentication_does_not_resolve_request(setup):
    ledger, _ = setup
    create(ledger)
    assert ledger.authenticate_operator(TOKEN) == {"operator_id": "operator-local",
        "local_operator_authenticated": True, "sso_identity_verified": False}
    assert ledger.status("r1")["receipt"] is None


@pytest.mark.parametrize("change", [{"request_sha256": "b"*64}, {"observed_state_sha256": "b"*64}])
def test_displayed_request_bound(setup, change):
    ledger, _ = setup
    request = create(ledger)
    with pytest.raises(InflightApprovalError, match="displayed_request_mismatch"):
        resolve(ledger, request, **change)


@pytest.mark.parametrize("delta", [
    {"add_commands": ["run_arbitrary_code"], "resource_limits": {}},
    {"add_commands": ["continue"], "resource_limits": {}},
    {"add_commands": ["resume", "resume"], "resource_limits": {}},
    {"add_commands": [], "resource_limits": {"hold_seconds": 60.01}},
    {"add_commands": [], "resource_limits": {"hold_seconds": 29.}},
    {"add_commands": [], "resource_limits": {"hold_seconds": True}},
    {"add_commands": [], "resource_limits": {"hold_seconds": float("nan")}},
    {"add_commands": [], "resource_limits": {"unregistered": 1}},
    {"add_commands": [], "resource_limits": {}},
    {"add_commands": [], "resource_limits": {}, "code": "pass"},
])
def test_scope_expansion_is_bounded(setup, delta):
    ledger, _ = setup
    with pytest.raises(InflightApprovalError):
        create(ledger, delta=delta)


def test_fallback_must_already_be_authorized(setup):
    ledger, _ = setup
    with pytest.raises(InflightApprovalError, match="fallback_not_preapproved"):
        create(ledger, fallback="resume")


@pytest.mark.parametrize("choice", ["reject", "approve"])
def test_late_consumption_uses_fallback_even_after_approval(setup, choice):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    resolve(ledger, request, choice=choice)
    clock[0] = 115.
    result = consume(ledger, observation=observation(25., 115.), simulation_time_s=25.)
    assert not result["approved"] and result["reason"] == "expired"
    assert result["scope"] == SCOPE and result["revision"] == 3 and result["fallback"] == "continue"


def test_reject_consumes_original_fallback(setup):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    resolve(ledger, request, choice="reject")
    clock[0] = 102.
    result = consume(ledger)
    assert result["reason"] == "rejected" and result["fallback"] == "continue"
    with pytest.raises(InflightApprovalError, match="already_resolved"):
        resolve(ledger, request)


@pytest.mark.parametrize("sim,wall", [(25., 102.), (12., 115.)])
def test_no_response_expires_on_either_original_deadline(setup, sim, wall):
    ledger, clock = setup
    create(ledger)
    clock[0] = wall
    result = consume(ledger, observation=observation(sim, wall), simulation_time_s=sim)
    assert result["reason"] == "expired"
    receipt = ledger.status("r1")["receipt"]
    assert receipt["choice"] == "expire" and receipt["operator_id"] is None


def test_pending_cannot_consume_before_deadline(setup):
    ledger, clock = setup
    create(ledger)
    clock[0] = 102.
    with pytest.raises(InflightApprovalError, match="response_pending"):
        consume(ledger)
    with pytest.raises(InflightApprovalError, match="deadline_not_reached"):
        ledger.expire(request_id="r1", simulation_time_s=12.)


def test_expire_is_idempotent_and_late_operator_cannot_revive_request(setup):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 115.
    receipt = ledger.expire(request_id="r1", simulation_time_s=25.)
    assert ledger.expire(request_id="r1", simulation_time_s=26.) == receipt
    with pytest.raises(InflightApprovalError, match="already_resolved"):
        resolve(ledger, request, simulation_time_s=25.)


@pytest.mark.parametrize("changes", [
    {"mission_id": "mission2"}, {"vehicle_id": "booster"}, {"revision": 4}, {"revision": True},
    {"scope": {"commands": ["hold"], "resource_limits": SCOPE["resource_limits"]}},
    {"observation": observation(9., 102.)}, {"observation": observation(13., 102.)},
    {"observation": observation(12., 99.)}, {"observation": observation(12., 103.)},
    {"simulation_time_s": 9., "observation": observation(9., 102.)}, {"run_active": False},
])
def test_consume_rechecks_context_clock_observation_and_liveness(setup, changes):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    resolve(ledger, request)
    clock[0] = 102.
    with pytest.raises(InflightApprovalError):
        consume(ledger, **changes)
    assert ledger.status("r1")["consumption"] is None


def test_request_mutation_and_duplicate_id_cannot_extend_deadline(setup):
    ledger, _ = setup
    request = create(ledger)
    request["wall_deadline_s"] = 999.
    assert ledger.status("r1")["request"]["wall_deadline_s"] == 115.
    with pytest.raises(InflightApprovalError, match="already_used"):
        create(ledger, wall_deadline_s=116.)


def test_tampered_persisted_receipt_is_rejected(setup):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    resolve(ledger, request, choice="reject")
    with sqlite3.connect(ledger._path) as db:
        raw = json.loads(db.execute("SELECT receipt FROM requests WHERE id='r1'").fetchone()[0])
        raw["choice"] = "approve"
        db.execute("UPDATE requests SET receipt=? WHERE id='r1'", (json.dumps(raw),))
    clock[0] = 102.
    with pytest.raises(InflightApprovalError, match="integrity_failure"):
        consume(ledger)


def test_signed_receipt_from_other_request_cannot_be_spliced(setup):
    ledger, clock = setup
    first = create(ledger)
    second = create(ledger, request_id="r2")
    clock[0] = 101.
    resolve(ledger, first, choice="reject")
    resolve(ledger, second, request_id="r2")
    with sqlite3.connect(ledger._path) as db:
        raw = db.execute("SELECT receipt FROM requests WHERE id='r2'").fetchone()[0]
        db.execute("UPDATE requests SET receipt=? WHERE id='r1'", (raw,))
    clock[0] = 102.
    with pytest.raises(InflightApprovalError, match="record_binding_failure"):
        consume(ledger)


def test_competing_consumers_receive_only_one_grant(setup):
    ledger, clock = setup
    request = create(ledger)
    clock[0] = 101.
    resolve(ledger, request)
    clock[0] = 102.
    def attempt(_):
        try:
            return consume(ledger)["approved"]
        except InflightApprovalError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count(True) == 1 and results.count("request_already_consumed") == 1


def test_reopened_ledger_cannot_change_registered_caps(setup):
    ledger, _ = setup
    altered = deepcopy(REGISTRY)
    altered["resource_limits"]["hold_seconds"] = 1000
    with pytest.raises(InflightApprovalError, match="configuration_mismatch"):
        InflightApprovalLedger(ledger._path, signing_key=KEY, operator_token=None,
            operator_id="operator-local", registry=altered)

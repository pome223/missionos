"""Real anonymous pipes/ledger with fake reservation callbacks; no physics/model."""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from src.runtime.starship_inflight_approval import InflightApprovalError, InflightApprovalLedger
from src.runtime.starship_inflight_runtime import (
    ApprovalPipeReader, HostApprovalBridge, WorkerApprovalActor, approval_pipe,
)

TOKEN = "explicit-fixture-local-operator-token-not-secret"
CONTEXT = {"run_id": "run1", "mission_id": "mission1", "vehicle_id": "ship"}
SCOPE = {"commands": ["select_nominal", "keep_reserved_return"], "resource_limits": {"maximum_return_delay_s": 6000.}}
REGISTRY = {"commands": SCOPE["commands"]+["select_next_orbit"], "resource_limits": SCOPE["resource_limits"]}


@pytest.fixture
def item(tmp_path):
    clock = [100.]
    state = {"run_active": True, "simulation_time_s": 10., "revision": 2, "scope": deepcopy(SCOPE),
        "reservation_sha256": "b"*64, "observation": {"state_sha256": "a"*64, "simulation_time_s": 10., "wall_time_s": 100.}}
    bounds = {"simulation_deadline_s": 25., "wall_deadline_s": 115., "maximum_observation_age_s": 2.}
    ledger = InflightApprovalLedger(tmp_path/"approval.sqlite", signing_key=b"host-only-fixture-integrity-key-32bytes",
        operator_token=TOKEN, operator_id="local-test-operator", registry=REGISTRY, clock=lambda: clock[0])
    requests, request_writer = approval_pipe()
    grants, grant_writer = approval_pipe()
    events, commands, checks = [], [], []
    eligible = {"accepted": True, "candidate_sha256": "c"*64}
    def event(name, **fields):
        events.append({"event": name, **fields})
    def admission(action, current):
        checks.append((action, current["simulation_time_s"]))
        return deepcopy(eligible) if action == "select_next_orbit" else {"accepted": True, "existing_reservation_only": True}
    def command(action, current, grant):
        commands.append((action, current["simulation_time_s"]))
        state.update(scope=deepcopy(grant["scope"]), revision=grant["revision"], reservation_sha256="d"*64)
        return {"applied": True, "revision": grant["revision"], "reservation_sha256": "d"*64}
    actor = WorkerApprovalActor(context=CONTEXT, scope=SCOPE, revision=2, request_writer=request_writer,
        grant_reader=grants, bounds=bounds, admission=admission, command=command, event=event, clock=lambda: clock[0])
    host = HostApprovalBridge(ledger=ledger, context=CONTEXT, snapshot=lambda: deepcopy(state),
        request_reader=requests, grant_writer=grant_writer, bounds=bounds, event=event, clock=lambda: clock[0])
    value = SimpleNamespace(clock=clock, state=state, bounds=bounds, ledger=ledger, actor=actor, host=host,
        events=events, commands=commands, checks=checks, eligible=eligible,
        requests=requests, request_writer=request_writer, grants=grants, grant_writer=grant_writer)
    yield value
    for endpoint in (requests, request_writer, grants, grant_writer):
        endpoint.close()


def advance(item, sim, wall=None):
    item.clock[0] = wall if wall is not None else 90.+sim
    item.state.update(simulation_time_s=sim, observation={"state_sha256": "e"*64,
        "simulation_time_s": sim, "wall_time_s": item.clock[0]})


def begin(item):
    item.actor.request_change(request_id="approval1", candidate_sha256="c"*64, state=deepcopy(item.state))
    item.host.tick()
    return item.ledger.status("approval1")["request"]


def resolve(item, choice="approve"):
    request = item.ledger.status("approval1")["request"]
    return item.ledger.resolve(request_id="approval1", request_sha256=request["sha256"],
        observed_state_sha256=request["observed_state_sha256"], choice=choice,
        bearer_token=TOKEN, simulation_time_s=item.state["simulation_time_s"], candidate_sha256=request["candidate_sha256"])


def test_physics_can_advance_while_approval_is_pending(item):
    begin(item)
    for sim in (11., 12., 13.):
        advance(item, sim)
        assert item.host.tick() is None
        assert item.actor.tick(deepcopy(item.state)) is None
    assert item.commands == [] and item.actor.status == "awaiting_operator"
    assert item.host.snapshot()["pending_request_id"] == "approval1"
    assert item.state["simulation_time_s"] == 13.


def test_approval_updates_reservation_then_requires_later_observation(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    host_result = item.host.tick()
    assert host_result["consumption"]["approved"]
    before = deepcopy(item.state)
    result = item.actor.tick(before)
    assert result["status"] == "reservation_changed_waiting_observation"
    assert result["burn_dispatch_issued"] is False and item.commands == [("select_next_orbit", 12.)]
    assert item.actor.confirm(deepcopy(item.state)) is None
    advance(item, 13.)
    assert item.actor.tick(deepcopy(item.state))["status"] == "reservation_confirmed"
    assert item.state["scope"]["resource_limits"] == SCOPE["resource_limits"]
    event = next(row for row in item.events if row["event"] == "m3_return_reservation_changed")
    assert not event["host_signature_verified_by_worker"] and not event["burn_dispatch_issued"]
    observed = item.events[-1]
    assert observed["matched"] and not observed["physical_return_verified"]
    assert item.actor.tick(deepcopy(item.state)) is None
    assert len(item.commands) == 1


def test_reject_keeps_existing_reservation_without_new_calculation(item):
    begin(item)
    advance(item, 12.)
    resolve(item, "reject")
    item.host.tick()
    result = item.actor.tick(deepcopy(item.state))
    assert result["status"] == "fallback_preserved" and not result["reservation_changed"]
    assert item.state["reservation_sha256"] == "b"*64 and item.commands == []
    assert item.checks == [("keep_reserved_return", 12.)]


@pytest.mark.parametrize("sim,wall", [(25., 103.), (13., 115.)])
def test_no_response_falls_back_on_original_deadline(item, sim, wall):
    begin(item)
    advance(item, sim, wall)
    item.host.tick()
    result = item.actor.tick(deepcopy(item.state))
    assert result["reason"] == "original_deadline_expired" and result["status"] == "fallback_preserved"
    assert item.commands == [] and item.state["scope"] == SCOPE
    assert item.ledger.status("approval1")["consumption"]["approved"] is False


def test_host_unavailable_does_not_block_physics_or_deadline_fallback(item):
    item.actor.request_change(request_id="approval1", candidate_sha256="c"*64, state=deepcopy(item.state))
    advance(item, 25.)
    result = item.actor.tick(deepcopy(item.state))
    assert result["status"] == "fallback_preserved" and item.commands == []


def test_revision_changed_invalidates_approval_and_preserves_current_reservation(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.state["revision"] = 3
    result = item.host.tick()
    assert result["consumption"] is None and result["reason"] == "current_context_changed"
    assert item.actor.tick(deepcopy(item.state))["reason"] == "runtime_context_changed"
    assert item.commands == []


@pytest.mark.parametrize("change", [{"accepted": False}, {"candidate_sha256": "f"*64}])
def test_candidate_is_independently_rechecked_after_approval(item, change):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    item.eligible.update(change)
    result = item.actor.tick(deepcopy(item.state))
    assert result["reason"] == "candidate_no_longer_admitted" and item.commands == []
    assert item.ledger.status("approval1")["consumption"]["approved"]
    assert item.actor.scope == SCOPE


def test_approved_host_result_cannot_cross_run_or_expand_deadline(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    result = item.grants.poll()
    result["context"]["run_id"] = "other"
    item.grant_writer.send(result)
    assert item.actor.tick(deepcopy(item.state))["reason"] == "unbound_host_result"
    assert item.commands == []


def test_invalid_update_ack_does_not_claim_reservation_unchanged_or_retry(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    def uncertain_command(*_args):
        item.state["reservation_sha256"] = "d"*64
        raise OSError("fixture failed after mutation")
    item.actor.command = uncertain_command
    result = item.actor.tick(deepcopy(item.state))
    assert result["status"] == "reservation_update_unconfirmed" and result["reservation_changed"] is None
    assert item.events[-1]["retry_authorized"] is False
    assert item.actor.tick(deepcopy(item.state)) is None


def test_confirmation_does_not_accept_same_tick_or_different_reservation(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    item.actor.tick(deepcopy(item.state))
    assert item.actor.confirm(deepcopy(item.state)) is None
    advance(item, 13.)
    item.state["reservation_sha256"] = "f"*64
    assert item.actor.confirm(deepcopy(item.state))["status"] == "reservation_confirmation_failed"


def test_worker_holds_no_operator_token_ledger_or_signing_key(item):
    begin(item)
    assert not {"ledger", "operator_token", "signing_key", "_key", "_token"} & vars(item.actor).keys()
    frame = item.host.frame
    assert TOKEN not in str(frame) and "signature" not in frame


def test_records_keep_host_binding_and_no_worker_signature_verification_claim(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    item.actor.tick(deepcopy(item.state))
    host, worker = item.host.record(), item.actor.record()
    assert host["request_frame"] == worker["request_frame"]
    assert host["result"] == worker["host_result"]
    assert not worker["host_signature_verified_by_worker"] and not worker["burn_dispatch_issued"]
    assert TOKEN not in str(host) and TOKEN not in str(worker)
    worker["status"] = "forged"
    assert item.actor.status != "forged"


def test_stale_post_update_observation_is_not_reported_as_confirmation(item):
    begin(item)
    advance(item, 12.)
    resolve(item)
    item.host.tick()
    item.actor.tick(deepcopy(item.state))
    item.clock[0] = 110.
    assert item.actor.confirm(deepcopy(item.state))["status"] == "reservation_confirmation_failed"
    assert item.events[-1]["physical_return_verified"] is False


def test_regular_file_cannot_substitute_for_inherited_host_pipe(tmp_path):
    path = tmp_path/"forged-host-result.json"
    path.write_text("{}\n")
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(InflightApprovalError, match="read_only_pipe_required"):
            ApprovalPipeReader(fd)
    finally:
        os.close(fd)


def test_pipe_reader_is_nonblocking_and_rejects_duplicate_json_keys(item):
    assert item.grants.poll() is None
    os.write(item.grant_writer.fd, b'{"key":1,"key":2}\n')
    with pytest.raises(InflightApprovalError, match="duplicate_pipe_key"):
        item.grants.poll()


def test_cannot_send_second_request_to_reset_deadline(item):
    begin(item)
    with pytest.raises(InflightApprovalError, match="budget_exhausted"):
        item.actor.request_change(request_id="approval2", candidate_sha256="c"*64, state=deepcopy(item.state))


def test_candidate_is_bound_through_request_receipt_consumption(item):
    request = begin(item)
    receipt = resolve(item)
    result = item.host.tick()
    assert request['candidate_sha256'] == receipt['candidate_sha256'] == result['consumption']['candidate_sha256'] == 'c'*64
    assert item.actor.tick(deepcopy(item.state))['reservation_changed']


def test_host_rejects_worker_observation_not_seen_by_host(item):
    state = deepcopy(item.state)
    state['observation']['state_sha256'] = 'f'*64
    item.actor.request_change(request_id='approval1', candidate_sha256='c'*64, state=state)
    rejected = item.host.tick()
    assert rejected['reason'] == 'invalid_worker_scope_request'
    assert item.actor.tick(deepcopy(item.state))['status'] == 'fallback_preserved'
    with pytest.raises(InflightApprovalError, match='request_not_found'):
        item.ledger.status('approval1')
    assert item.commands == []


def test_host_accepts_only_fresh_observation_from_trusted_bounded_history(item):
    original = deepcopy(item.state['observation'])
    item.actor.request_change(request_id='approval1', candidate_sha256='c'*64, state=deepcopy(item.state))
    advance(item, 11.)
    item.state['known_observations'] = [original]
    assert item.host.tick() is None
    assert item.host.request['observed_state_sha256'] == original['state_sha256']
    assert 'known_observations' not in item.host.snapshot()


def test_host_history_does_not_make_stale_worker_observation_fresh(item):
    original = deepcopy(item.state['observation'])
    item.actor.request_change(request_id='approval1', candidate_sha256='c'*64, state=deepcopy(item.state))
    advance(item, 13.)
    item.state['known_observations'] = [original]
    rejected = item.host.tick()
    assert rejected['reason'] == 'invalid_worker_scope_request'
    assert item.actor.tick(deepcopy(item.state))['status'] == 'fallback_preserved'
    assert item.commands == []


@pytest.mark.parametrize('malformed', [[], 'invalid', 3, {'approved': 1}])
def test_malformed_consumption_preserves_reservation_instead_of_crashing(item, malformed):
    begin(item)
    resolve(item)
    item.host.tick()
    result = item.grants.poll()
    result['consumption'] = malformed
    item.grant_writer.send(result)
    answer = item.actor.tick(deepcopy(item.state))
    assert answer['status'] == 'fallback_preserved'
    assert answer['reason'] == 'invalid_host_consumption_shape'
    assert item.commands == [] and item.state['reservation_sha256'] == 'b'*64


def test_candidate_binding_cannot_be_replaced_with_another_consumption(item):
    from src.runtime.starship_inflight_approval import digest
    begin(item)
    resolve(item)
    item.host.tick()
    result = item.grants.poll()
    result['consumption']['candidate_sha256'] = 'f'*64
    result['consumption']['sha256'] = digest({k: v for k, v in result['consumption'].items() if k not in ('sha256', 'signature')})
    item.grant_writer.send(result)
    assert item.actor.tick(deepcopy(item.state))['reason'] == 'invalid_host_consumption_binding'
    assert item.commands == []


@pytest.mark.parametrize('kind', ['malformed', 'exception'])
def test_admission_adapter_failure_never_issues_reservation_command(item, kind):
    begin(item)
    resolve(item)
    item.host.tick()
    def broken_admission(action, _state):
        if action == 'keep_reserved_return':
            return {'accepted': True}
        if kind == 'exception':
            raise RuntimeError('not exposed diagnostic content')
        return []
    item.actor.admission = broken_admission
    answer = item.actor.tick(deepcopy(item.state))
    assert answer['status'] == 'fallback_preserved' and item.commands == []
    assert 'not exposed' not in str(item.actor.record())


@pytest.mark.parametrize('kind', ['malformed', 'exception'])
def test_command_adapter_failure_is_unknown_and_not_retried(item, kind):
    begin(item)
    resolve(item)
    item.host.tick()
    calls = []
    def broken_command(*_args):
        calls.append(True)
        item.state['reservation_sha256'] = 'd'*64
        if kind == 'exception':
            raise RuntimeError('not exposed command content')
        return []
    item.actor.command = broken_command
    answer = item.actor.tick(deepcopy(item.state))
    assert answer['status'] == 'reservation_update_unconfirmed'
    assert answer['reservation_changed'] is None
    assert item.actor.tick(deepcopy(item.state)) is None and calls == [True]
    assert 'not exposed' not in str(item.actor.record())


def test_consumed_approval_is_explicitly_pending_even_if_worker_rejects(item):
    begin(item)
    resolve(item)
    item.host.tick()
    item.eligible['accepted'] = False
    assert item.actor.tick(deepcopy(item.state))['status'] == 'fallback_preserved'
    grant = item.ledger.status('approval1')['consumption']
    assert grant['approved']
    assert grant['approval_disposition'] == 'approved_pending_executor'
    assert grant['scope_applied'] is False
    assert grant['revision_semantics'] == 'authorized_revision_not_applied_revision'
    assert item.actor.revision == 2 and item.actor.applied is None


def test_host_malformed_request_returns_bounded_rejection_without_raising(item):
    item.actor.request_change(request_id='approval1', candidate_sha256='c'*64, state=deepcopy(item.state))
    item.requests.poll()
    os.write(item.request_writer.fd, b'{"schema":"wrong","schema":"duplicate"}\n')
    rejection = item.host.tick()
    assert rejection['schema'] == 'missionos.inflight_host_rejection.v1'
    assert rejection['reason'] == 'invalid_worker_scope_request'
    assert item.actor.tick(deepcopy(item.state))['status'] == 'fallback_preserved'
    assert item.commands == [] and item.host.finished
    assert item.host.tick() is None


def test_reserved_return_cutoff_bounds_approval_deadline(item):
    from src.runtime.starship_inflight_runtime import _bounds
    _bounds({**item.bounds, 'reservation_cutoff_s': 25.})
    with pytest.raises(InflightApprovalError, match='reserved_return_cutoff'):
        _bounds({**item.bounds, 'reservation_cutoff_s': 24.99})
    for value in (True, float('nan'), -1.):
        with pytest.raises(InflightApprovalError, match='invalid_runtime_deadline_bounds'):
            _bounds({**item.bounds, 'reservation_cutoff_s': value})

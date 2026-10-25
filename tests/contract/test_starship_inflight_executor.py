"""Actual base select and inherited pipes, stub physics/admission; no model/6DOF."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.runtime.starship_inflight_approval import InflightApprovalLedger, digest
from src.runtime.starship_inflight_executor import InflightReplanningExecutor, verify_inflight_return
from src.runtime.starship_inflight_runtime import HostApprovalBridge, approval_pipe
from src.runtime.starship_replanning_executor import ReplanningExecutor
import src.runtime.starship_inflight_executor as module

TOKEN = "fixture-inflight-local-operator-token"


@pytest.fixture
def item(monkeypatch, tmp_path):
    clock, snapshots = [100.], []
    actor = object.__new__(InflightReplanningExecutor)
    actor._inflight_ready = False
    actor.run_id, actor.case, actor.s = "m3-fixture", "fixture", SimpleNamespace(time_s=1000.)
    actor.envelope = {"maximum_delay_s": 6000., "maximum_plan_revisions": 4,
                      "decision_timeout_s": 35.,
                      "event_supervision": {"deadline_margin_s": 5.}}
    actor.p = {"integration": {"coast_dt_s": 1.}}
    actor.times = {"nominal": 1050., "next_orbit": 1200.}
    actor.candidates = {name: {"candidate_id": name, "return_time_s": when, "fixture_value": 1}
                        for name, when in actor.times.items()}
    actor.selected, actor.revision, actor.dispatch = None, 0, None
    actor.revisions, actor.events = [], []
    actor.decisions = [{"request": {"request_id": "decision-0"}, "accepted_action": "select_nominal"}]
    actor.observation = lambda: {"state": {"time_s": actor.s.time_s}, "basis": "synthetic_navigation_estimate_v1"}
    eligible = {"value": True}
    monkeypatch.setattr(ReplanningExecutor, "admit_candidate", lambda *_: {"accepted": eligible["value"], "reasons": []})
    monkeypatch.setattr(module, "notices", lambda *_: {"sequence": 1})
    import src.runtime.starship_replanning_executor as base
    monkeypatch.setattr(base, "notices", lambda *_: {"sequence": 1})
    def step(self):
        self.s.time_s += 1.
        clock[0] += 1.
    monkeypatch.setattr(ReplanningExecutor, "step", step)
    assert ReplanningExecutor.select(actor, "nominal", "fixture")
    scope = {"commands": ["select_nominal", "keep_reserved_return"], "resource_limits": {"maximum_return_delay_s": 6000.}}
    contract = {"schema": "missionos.inflight_executor_contract.v1",
        "context": {"run_id": actor.run_id, "mission_id": "mission-1", "vehicle_id": "ship"},
        "initial_scope": scope,
        "bounds": {"simulation_deadline_s": 1010., "wall_deadline_s": 110.,
                   "maximum_observation_age_s": 2., "reservation_cutoff_s": 1045.}}
    reads, writes = approval_pipe()
    grants, grant_writer = approval_pipe()
    actor.connect_approval(inflight=contract, request_writer=writes, grant_reader=grants,
                           snapshot_sink=lambda value: snapshots.append(deepcopy(value)), clock=lambda: clock[0])
    ledger = InflightApprovalLedger(tmp_path/"ledger.sqlite", signing_key=b"fixture-only-signing-key-32-bytes!",
        operator_token=TOKEN, operator_id="scripted-test-operator", registry={"commands": scope["commands"]+["select_next_orbit"],
        "resource_limits": scope["resource_limits"]}, clock=lambda: clock[0])
    def snapshot():
        value = deepcopy(snapshots[-1])
        value["known_observations"] = [s["observation"] for s in snapshots[-64:]]
        return value
    host = HostApprovalBridge(ledger=ledger, context=contract["context"], snapshot=snapshot,
        request_reader=reads, grant_writer=grant_writer, bounds=contract["bounds"], clock=lambda: clock[0])
    control = SimpleNamespace(choice="approve", mutate=None)
    def wait(_label, poll, _limit, *, cutoff_time_s):
        host.tick()
        request = ledger.status(actor.approval_actor.frame["request_id"])["request"]
        if control.choice:
            ledger.resolve(request_id=request["request_id"], request_sha256=request["sha256"],
                observed_state_sha256=request["observed_state_sha256"], choice=control.choice,
                candidate_sha256=request["candidate_sha256"], bearer_token=TOKEN, simulation_time_s=actor.s.time_s)
        if control.mutate:
            control.mutate()
        host.tick()
        value = poll()
        if control.choice:
            actor.step()
        else:
            while actor.s.time_s < cutoff_time_s:
                actor.step()
            host.tick()
        return value
    actor.wait = wait
    value = SimpleNamespace(actor=actor, clock=clock, snapshots=snapshots, ledger=ledger, host=host,
        contract=contract, eligible=eligible, control=control)
    yield value
    for endpoint in (reads, writes, grants, grant_writer):
        endpoint.close()


def propose(item):
    item.actor.decisions.append({"request": {"request_id": "decision-1"}, "accepted_action": "select_next_orbit"})
    return item.actor.select("next_orbit", "fixture")


def study(item):
    actor = item.actor
    return {"run_id": actor.run_id, "envelope": deepcopy(actor.envelope), "opportunities": deepcopy(actor.times),
        "decisions": deepcopy(actor.decisions), "plan_revisions": deepcopy(actor.revisions),
        "dispatch": deepcopy(actor.dispatch), "candidates": deepcopy(actor.candidates),
        "execution": {"events": deepcopy(actor.events)}, "inflight_approval": {
            "contract": deepcopy(item.contract), "contract_sha256": digest(item.contract),
            "worker": actor.approval_actor.record(), "snapshots": deepcopy(actor._approval_snapshots), "active_scope": deepcopy(actor.active_scope),
            "scope_revision": actor.scope_revision, "authorized_candidate_sha256": actor._authorized_candidate}}


def verify(item, value=None):
    frame = item.actor.approval_actor.frame
    ledger = item.ledger.status(frame["request_id"]) if frame else None
    return verify_inflight_return(study(item) if value is None else value, expected_contract=item.contract,
        host_record=item.host.record(), signed_ledger_record=ledger)


def test_technical_eligibility_does_not_authorize_selection_or_dispatch(item):
    actor = item.actor
    assert actor.checks({})["next_orbit"]["accepted"]
    assert not actor.select("next_orbit", "checked_fallback")
    assert actor.selected == "nominal" and actor.approval_actor.status == "idle"
    actor.selected, actor.s.time_s = "next_orbit", 1200.
    assert not actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]
    assert actor.scope_revision == 0 and actor.dispatch is None


def test_consumed_approval_changes_actual_reservation_then_later_observation(item):
    assert propose(item)
    actor = item.actor
    assert actor.selected == "next_orbit" and actor.revision == 2 and actor.scope_revision == 1
    assert actor.approval_actor.status == "reservation_confirmed"
    assert actor.revisions[-1]["candidate_sha256"] == digest(actor.candidates["next_orbit"])
    assert actor.revisions[-1]["decision_request_id"] == "decision-1"
    assert actor.dispatch is None
    verdict = verify(item)
    assert verdict["passed"] and verdict["scope_amendment_applied"] and verdict["reservation_observed"]
    assert not verdict["burn_dispatch_observed"] and not verdict["physical_return_verified"]


@pytest.mark.parametrize("choice", ["reject", None])
def test_rejection_or_no_answer_preserves_nominal_without_expansion(item, choice):
    item.control.choice = choice
    assert not propose(item)
    assert item.actor.selected == "nominal" and item.actor.revision == 1 and item.actor.scope_revision == 0
    assert item.actor.approval_actor.status == "fallback_preserved"
    assert verify(item)["passed"]


@pytest.mark.parametrize("mutation", ["candidate", "admission", "reservation", "scope_revision"])
def test_consumed_approval_is_not_execution_if_state_changed(item, mutation):
    def mutate():
        actor = item.actor
        if mutation == "candidate":
            actor.candidates["next_orbit"]["fixture_value"] = 2
        elif mutation == "admission":
            item.eligible["value"] = False
        elif mutation == "reservation":
            actor.revision += 1
        else:
            actor.scope_revision += 1
    item.control.mutate = mutate
    assert not propose(item)
    assert item.actor.selected == "nominal" and item.actor._authorized_candidate is None
    assert item.actor.dispatch is None


def test_dispatch_requires_same_candidate_confirmed_and_correct_burn_time(item):
    assert propose(item)
    actor = item.actor
    assert not actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]
    actor.s.time_s = 1200.
    assert actor.dispatch_check("next_orbit", {}, actor.observation()) == {"accepted": True, "reasons": []}
    actor.candidates["next_orbit"]["fixture_value"] = 3
    assert not actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]


def test_single_use_grant_cannot_reserve_another_candidate_or_be_reused(item):
    assert propose(item)
    actor = item.actor
    assert actor.select("next_orbit", "fixture")
    assert len(actor.revisions) == 2
    actor.decisions.append({"request": {"request_id": "decision-2"}, "accepted_action": "select_nominal"})
    assert actor.select("nominal", "fixture")
    assert not actor.select("next_orbit", "fixture")
    assert actor.selected == "nominal" and len(actor.revisions) == 3


def test_unknown_or_forged_scope_cannot_sneak_through_final_gate(item):
    actor = item.actor
    actor.active_scope["commands"].append("select_next_orbit")
    actor.selected, actor.s.time_s = "next_orbit", 1200.
    assert not actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]


def test_expired_attempt_cannot_reset_deadlines_or_retry(item):
    item.control.choice = None
    assert not propose(item)
    original = deepcopy(item.actor.approval_actor.frame)
    assert not item.actor.select("next_orbit", "fixture")
    assert item.actor.approval_actor.frame == original


def test_scope_revision_is_distinct_from_plan_revision(item):
    assert item.actor.revision == 1 and item.actor.scope_revision == 0
    assert propose(item)
    assert item.actor.revision == 2 and item.actor.scope_revision == 1
    public = item.actor.decision_context()["active_authority"]
    assert public["scope_revision"] == 1 and public["contract_sha256"] == digest(item.contract)
    assert public["technical_candidates_are_not_authorization"]
    assert TOKEN not in str(public) and "signature" not in str(public)


@pytest.mark.parametrize("mutation", ["hash", "receipt_only", "wrong_ledger", "no_later_observation"])
def test_independent_authority_audit_rejects_forged_or_incomplete_evidence(item, mutation):
    assert propose(item)
    value = study(item)
    if mutation == "hash":
        value["plan_revisions"][-1]["candidate_sha256"] = "0"*64
    elif mutation == "receipt_only":
        value["plan_revisions"] = value["plan_revisions"][:1]
    elif mutation == "wrong_ledger":
        value["inflight_approval"]["worker"]["host_result"]["consumption"]["candidate_sha256"] = "0"*64
    else:
        value["inflight_approval"]["worker"]["events"] = []
    assert not verify(item, value)["passed"]


def test_baseline_hooks_do_not_add_authority_or_modify_physical_check(item):
    assert ReplanningExecutor.decision_context(item.actor) == {}
    assert ReplanningExecutor.next_monitor_time(item.actor, 1234.) == 1234.
    request = {"allowed_actions": ["select_nominal"]}
    assert ReplanningExecutor.constrain_decision_request(item.actor, request) is request
    assert ReplanningExecutor.dispatch_check(item.actor, "nominal", {}, item.actor.observation()) == {"accepted": True, "reasons": []}


def _early_authority_window(item):
    actor = item.actor
    actor.s.time_s, item.clock[0] = 900., 0.
    actor._approval_snapshots.clear()
    item.snapshots.clear()
    actor._publish_snapshot()
    actor.scheduled = 1000.
    return actor


def _timed_request(actor):
    return actor.constrain_decision_request({"request_id": "decision-window", "time_s": actor.s.time_s,
        "deadline_s": actor.s.time_s+35., "allowed_actions": ["select_nominal", "select_next_orbit", "wait_for_update"],
        **actor.decision_context()})


def test_wait_wakes_with_model_and_operator_time_left_after_discrete_coast(item):
    actor = _early_authority_window(item)
    due = actor.next_monitor_time(1200.)
    # The actual production coast loop advances the stub plant in discrete ticks.
    actor.coast_to(due)
    request = _timed_request(actor)
    window = request["active_authority"]["request_window"]
    assert due <= actor.s.time_s < due+actor.p["integration"]["coast_dt_s"]
    assert "select_next_orbit" in request["allowed_actions"]
    assert window["request_available"]
    assert request["deadline_s"]+window["operator_response_reserve_s"] < window["simulation_deadline_s"]
    # Choosing wait again does not create a zero-time/repeated deadline loop.
    assert actor.next_monitor_time(1200.) == 1200.


def test_last_scheduled_decision_can_use_full_model_window_then_confirm_approval(item):
    actor = _early_authority_window(item)
    actor.coast_to(actor.next_monitor_time(1200.))
    request = _timed_request(actor)
    assert "select_next_orbit" in request["allowed_actions"]
    actor.coast_to(request["deadline_s"])
    assert propose(item)
    assert actor.approval_actor.status == "reservation_confirmed"
    assert actor.scope_revision == 1 and actor.dispatch is None
    assert verify(item)["passed"]


def test_wall_limited_warning_is_delivered_once_and_does_not_loop(item):
    actor = _early_authority_window(item)
    for contract in (item.contract, actor.inflight_contract):
        contract["bounds"]["wall_deadline_s"] = 100.
    actor.approval_actor.bounds["wall_deadline_s"] = 100.
    due = actor.next_monitor_time(1200.)
    actor.coast_to(due)
    request = _timed_request(actor)
    assert request["active_authority"]["request_window"]["request_available"]
    assert "select_next_orbit" in request["allowed_actions"]
    assert actor.next_monitor_time(1200.) == 1200.


@pytest.mark.parametrize("clock_kind", ["simulation", "wall"])
def test_expired_request_is_not_advertised_and_cannot_be_sent(item, clock_kind):
    actor = _early_authority_window(item)
    if clock_kind == "simulation":
        actor.s.time_s = item.contract["bounds"]["simulation_deadline_s"]
    else:
        item.clock[0] = item.contract["bounds"]["wall_deadline_s"]
    request = _timed_request(actor)
    window = request["active_authority"]["request_window"]
    assert window["reason"] == "original_deadline_expired"
    assert "select_next_orbit" not in request["allowed_actions"]
    assert "select_nominal" in request["allowed_actions"]
    assert not propose(item)
    assert actor.approval_actor.frame is None and actor.scope_revision == 0
    assert actor.next_monitor_time(1200.) == 1200.


def test_late_response_cannot_turn_an_earlier_viable_option_into_authority(item):
    actor = _early_authority_window(item)
    assert "select_next_orbit" in _timed_request(actor)["allowed_actions"]
    actor.coast_to(item.contract["bounds"]["simulation_deadline_s"]+1.)
    assert not propose(item)
    assert actor.approval_actor.frame is None and actor.selected == "nominal"


def test_too_short_window_removes_only_unapproved_candidate_not_physical_evidence(item):
    actor = _early_authority_window(item)
    actor.s.time_s = 1000.
    request = _timed_request(actor)
    assert request["active_authority"]["request_window"]["reason"] == "insufficient_model_and_operator_time"
    assert "select_next_orbit" not in request["allowed_actions"]
    assert actor.checks({})["next_orbit"]["accepted"]
    assert actor.selected == "nominal"


@pytest.mark.parametrize("mutation", ["deadline", "remaining", "wall", "advertised"])
def test_scalar_audit_binds_request_window_to_contract_and_observations(item, mutation):
    actor = _early_authority_window(item)
    request = _timed_request(actor)
    actor.decisions = [{"request": request, "accepted_action": "wait_for_update"}]
    actor.step()
    value = study(item)
    assert verify(item, value)["passed"]
    timed = value["decisions"][0]["request"]
    window = timed["active_authority"]["request_window"]
    if mutation == "deadline":
        window["simulation_deadline_s"] += 1
    elif mutation == "remaining":
        window["remaining_simulation_s"] += 1
    elif mutation == "wall":
        from src.runtime.starship_inflight_runtime import amendment_window
        timed["active_authority"]["request_window"] = amendment_window(item.contract["bounds"],
            simulation_time_s=timed["time_s"], wall_time_s=10., model_response_reserve_s=35.,
            status="idle", scope_revision=0)
    else:
        timed["deadline_s"] = item.contract["bounds"]["simulation_deadline_s"]
    assert not verify(item, value)["passed"]


def test_nominal_absent_can_request_but_no_answer_is_explicitly_unresolved(item):
    item.actor.selected = None
    item.control.choice = None
    assert not propose(item)
    assert item.actor.approval_actor.status == "fallback_not_admitted"
    assert item.actor.selected is None and item.actor.dispatch is None


def test_pure_preflight_rejects_before_base_constructor(item, monkeypatch, tmp_path):
    from src.runtime.starship_inflight_executor import validate_inflight_executor_contract
    assert validate_inflight_executor_contract(item.contract, item.actor.envelope,
        run_id=item.actor.run_id) == item.contract
    monkeypatch.setattr(ReplanningExecutor, "__init__", lambda *_a, **_k: pytest.fail("physics prefix started"))
    invalid = deepcopy(item.contract)
    invalid["initial_scope"]["commands"].append("select_next_orbit")
    with pytest.raises(ValueError, match="initial_return_scope"):
        InflightReplanningExecutor({}, item.actor.envelope, "fixture", tmp_path,
            run_id=item.actor.run_id, inflight=invalid, request_writer=None, grant_reader=None, snapshot_sink=lambda _: None)


@pytest.mark.parametrize("field", ["run_id", "mission_id", "vehicle_id"])
def test_scalar_audit_binds_host_evidence_to_outer_contract(item, field):
    assert propose(item)
    value = study(item)
    host = item.host.record()
    frame = value["inflight_approval"]["worker"]["request_frame"]
    response = value["inflight_approval"]["worker"]["host_result"]
    host["context"][field] = frame["context"][field] = response["context"][field] = "other"
    host["request_frame"] = deepcopy(frame)
    host["result"] = deepcopy(response)
    ledger = item.ledger.status(frame["request_id"])
    verdict = verify_inflight_return(value, expected_contract=item.contract, host_record=host, signed_ledger_record=ledger)
    assert not verdict["passed"] and "host_evidence_outside_original_contract" in verdict["issues"]


def test_later_receipt_without_actual_executor_snapshot_is_rejected(item):
    assert propose(item)
    value = study(item)
    value["inflight_approval"]["snapshots"] = value["inflight_approval"]["snapshots"][:1]
    assert not verify(item, value)["passed"]


def test_broken_request_channel_does_not_retry_single_attempt(item):
    calls = []
    def broken(_frame):
        calls.append(True)
        raise OSError("fixture closed channel")
    item.actor.approval_actor.writer.send = broken
    assert not propose(item)
    assert not item.actor.select("next_orbit", "fixture")
    assert calls == [True] and item.actor.selected == "nominal"


def test_scope_request_is_not_sent_if_snapshot_forwarding_fails(item):
    def broken(_snapshot):
        raise OSError("fixture telemetry closed")
    item.actor._snapshot_sink = broken
    assert not propose(item)
    assert item.actor.approval_actor.frame is None


def test_fixture_policy_is_fixed_and_cannot_opt_in_live_inference(item):
    from src.runtime.starship_inflight_executor import FIXTURE_POLICY, validate_inflight_executor_contract
    contract = deepcopy(item.contract)
    contract["fixture_policy"] = deepcopy(FIXTURE_POLICY)
    envelope = {**item.actor.envelope, "mode": "fixture"}
    assert validate_inflight_executor_contract(contract, envelope, run_id=item.actor.run_id) == contract
    with pytest.raises(ValueError, match="unregistered_inflight_fixture_policy"):
        validate_inflight_executor_contract(contract, {**envelope, "mode": "live"}, run_id=item.actor.run_id)


@pytest.mark.parametrize("approved", [False, True])
def test_production_run_dispatch_branch_is_gated_by_approval(item, monkeypatch, approved):
    import src.runtime.starship_replanning_executor as base
    actor = item.actor
    if approved:
        assert propose(item)
    else:
        actor.selected = "next_orbit"  # An invalid reservation must not arm a burn.
    actor.monitor, actor.sources = None, {}
    actor.s.time_s, actor.scheduled, actor.contact = 1160., 1050., None
    actor.controller = SimpleNamespace(return_time_s=1e9)
    actor.choose = lambda *_: "keep_plan"
    actor.calculate = lambda *_: True
    actor.plan_return = lambda: True
    actor.coast_to = lambda when: setattr(actor.s, "time_s", when)
    actor.finish = lambda: {"dispatch": actor.dispatch, "events": actor.events}
    monkeypatch.setattr(base, "source_hashes", lambda: {})
    def contact_step():
        actor.s.time_s += 1.
        actor.contact = {"fixture_contact": True}
    actor.step = contact_step
    result = ReplanningExecutor.run(actor)
    assert (result["dispatch"] is not None) is approved
    assert (actor.controller.return_time_s == 1200.) is approved
    assert bool(actor.contact) is approved
    assert any(e["event"] == "m3_return_dispatch_authority_checked" for e in result["events"])


@pytest.mark.parametrize("entry", ["calculate", "calculate_trials"])
def test_consumed_candidate_cannot_be_overwritten_by_new_forecasts(item, monkeypatch, entry):
    actor = item.actor
    assert propose(item)
    original = deepcopy(actor.candidates["next_orbit"])
    revision = deepcopy(actor.revisions[-1])
    calls = []
    def recalculate(self, names, **kwargs):
        calls.append((list(names), kwargs))
        for name in names:
            self.candidates[name] = {"replacement_prediction": True}
        return True
    monkeypatch.setattr(ReplanningExecutor, entry, recalculate)
    assert getattr(actor, entry)(["nominal", "next_orbit"], budget_s=3.)
    assert calls == [(["nominal"], {"budget_s": 3.})]
    assert actor.candidates["next_orbit"] == original
    assert digest(actor.candidates["next_orbit"]) == actor._authorized_candidate
    assert actor.revisions[-1] == revision and actor.scope_revision == 1
    assert getattr(actor, entry)(["next_orbit"])
    assert len(calls) == 1
    assert actor.last_calculation_result["reason"] == "candidate_bound_approval_preserved"
    actor.s.time_s = actor.times["next_orbit"]
    assert actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]
    item.eligible["value"] = False
    assert not actor.dispatch_check("next_orbit", {}, actor.observation())["accepted"]


@pytest.mark.parametrize("entry", ["calculate", "calculate_trials"])
def test_before_approval_candidate_computation_remains_available(item, monkeypatch, entry):
    calls = []
    monkeypatch.setattr(ReplanningExecutor, entry, lambda _self, names, **_kw: calls.append(names) or True)
    assert getattr(item.actor, entry)(["next_orbit"])
    assert calls == [["next_orbit"]]


def test_scalar_verifier_rejects_approved_candidate_replacement_even_without_dispatch(item):
    assert propose(item)
    value = study(item)
    value["candidates"]["next_orbit"]["fixture_value"] += 1
    verdict = verify(item, value)
    assert not verdict["passed"]
    assert "approved_prediction_replaced_after_consumption" in verdict["issues"]


@pytest.mark.parametrize("know_burn", [False, True])
def test_pure_preflight_rejects_deadline_between_cutoff_and_burn(item, know_burn):
    contract = deepcopy(item.contract)
    contract["bounds"]["simulation_deadline_s"] = 1046.
    with pytest.raises(ValueError, match="approval_deadline_exceeds_reserved_return_cutoff"):
        module.validate_inflight_executor_contract(contract, item.actor.envelope,
            run_id=item.actor.run_id, nominal_burn_s=1050. if know_burn else None)
    contract["bounds"]["simulation_deadline_s"] = 1045.
    assert module.validate_inflight_executor_contract(contract, item.actor.envelope,
        run_id=item.actor.run_id, nominal_burn_s=1050. if know_burn else None) == contract


def test_snapshot_references_booked_revision_without_traversing_mutable_candidate(item, monkeypatch):
    actor = item.actor
    booked = deepcopy(actor.revisions[-1])
    # Non-JSON data makes any accidental candidate serialization fail. This is
    # not an admissible prediction; snapshots still describe the old booking.
    actor.candidates['nominal']['trials'] = [{'coast': object()}]
    actor.revisions[-1]['candidate_snapshot']['unused_large_evidence'] = object()
    original_digest = module.digest
    digest_inputs, reservations = [], []
    original_reservation = actor.reservation
    def reservation():
        reservations.append(True)
        return original_reservation()
    def small_digest(value):
        if isinstance(value, dict):
            assert 'trials' not in value and 'candidate_snapshot' not in value
        digest_inputs.append(value)
        return original_digest(value)
    monkeypatch.setattr(actor, 'reservation', reservation)
    monkeypatch.setattr(module, 'digest', small_digest)
    snapshot = actor.approval_snapshot()
    assert reservations == [True]
    assert len(digest_inputs) == 2  # Small reservation and current observation.
    assert snapshot['reservation'] == {
        'candidate_id': 'nominal', 'candidate_sha256': booked['candidate_sha256'],
        'return_time_s': booked['candidate_snapshot']['return_time_s'],
        'plan_revision': booked['revision']}
    assert snapshot['reservation_sha256'] == original_digest(snapshot['reservation'])
    snapshot['reservation']['candidate_sha256'] = '0' * 64
    assert actor.reservation()['candidate_sha256'] == booked['candidate_sha256']


@pytest.mark.parametrize('name', ['nominal', 'next_orbit'])
@pytest.mark.parametrize('mutation', ['value', 'nested', 'same_id_reforecast', 'schedule', 'candidate_and_schedule'])
def test_cached_reservation_never_authorizes_changed_candidate_at_dispatch(item, name, mutation):
    actor = item.actor
    actor.candidates[name]['trials'] = [{'coast': [{'propellant_kg': 90000.}]}]
    if name == 'next_orbit':
        assert propose(item)
    else:
        assert actor.select(name, 'fixture')
    booked = actor.reservation()
    actor.s.time_s = actor.times[name]
    assert actor.dispatch_check(name, {}, actor.observation())['accepted']
    if mutation == 'value':
        actor.candidates[name]['fixture_value'] = 2
    elif mutation == 'nested':
        actor.candidates[name]['trials'][0]['coast'][0]['propellant_kg'] -= 1.
    elif mutation == 'same_id_reforecast':
        actor.candidates[name] = {**deepcopy(actor.candidates[name]), 'new_prediction_generation': 2}
    elif mutation == 'schedule':
        actor.times[name] += 1.
    else:
        actor.times[name] += 1.
        actor.candidates[name]['return_time_s'] = actor.times[name]
        actor.s.time_s = actor.times[name]
    assert actor.approval_snapshot()['reservation'] == booked
    verdict = actor.dispatch_check(name, {}, actor.observation())
    assert not verdict['accepted']
    assert actor.events[-1]['event'] == 'm3_return_dispatch_authority_checked'
    assert not actor.events[-1]['check']['accepted']
    assert actor.dispatch is None


def test_same_name_new_prediction_needs_new_checked_selection_revision(item):
    actor = item.actor
    booked = actor.reservation()
    actor.candidates['nominal'] = {**actor.candidates['nominal'], 'new_prediction_generation': 2}
    actor.s.time_s = actor.times['nominal']
    assert not actor.dispatch_check('nominal', {}, actor.observation())['accepted']
    assert actor.select('nominal', 'fixture')
    revised = actor.reservation()
    assert revised['plan_revision'] == booked['plan_revision'] + 1
    assert revised['candidate_sha256'] == digest(actor.candidates['nominal'])
    assert revised['candidate_sha256'] != booked['candidate_sha256']
    assert actor.dispatch_check('nominal', {}, actor.observation())['accepted']


def test_booked_identity_does_not_replace_fresh_dispatch_admission(item):
    actor = item.actor
    actor.s.time_s = actor.times['nominal']
    booked = actor.reservation()
    item.eligible['value'] = False
    assert not actor.dispatch_check('nominal', {}, actor.observation())['accepted']
    assert actor.reservation() == booked


def test_missing_selected_revision_stays_unresolved_without_hashing_current_candidate(item):
    actor = item.actor
    actor.revision += 1
    snapshot = actor.approval_snapshot()
    assert snapshot['reservation']['candidate_sha256'] is None
    actor.s.time_s = actor.times['nominal']
    assert not actor.dispatch_check('nominal', {}, actor.observation())['accepted']


def test_nested_candidate_change_during_approval_still_blocks_consumed_grant(item):
    item.actor.candidates['next_orbit']['trials'] = [{'coast': [{'propellant_kg': 90000.}]}]
    def mutate():
        item.actor.candidates['next_orbit']['trials'][0]['coast'][0]['propellant_kg'] -= 1.
    item.control.mutate = mutate
    assert not propose(item)
    assert item.actor.selected == 'nominal'
    assert item.actor._authorized_candidate is None
    assert item.actor.dispatch is None


@pytest.mark.parametrize('choice', ['reject', None])
@pytest.mark.parametrize('mutation', ['same_id_reforecast', 'nested', 'missing_revision', 'schedule'])
def test_fallback_does_not_claim_preserved_booking_after_evidence_changes(item, choice, mutation):
    actor = item.actor
    actor.candidates['nominal']['trials'] = [{'coast': [{'propellant_kg': 90000.}]}]
    assert actor.select('nominal', 'fixture')
    item.control.choice = choice
    def mutate():
        if mutation == 'same_id_reforecast':
            actor.candidates['nominal'] = {**actor.candidates['nominal'], 'new_prediction_generation': 2}
        elif mutation == 'nested':
            actor.candidates['nominal']['trials'][0]['coast'][0]['propellant_kg'] -= 1.
        elif mutation == 'missing_revision':
            actor.revision += 1
        else:
            actor.times['nominal'] += 1.
    item.control.mutate = mutate
    assert not propose(item)
    assert actor.approval_actor.status == 'fallback_not_admitted'
    assert actor._authorized_candidate is None and actor.dispatch is None
    fallback_events = [e for e in actor.approval_actor.events if 'fallback_check' in e]
    assert fallback_events[-1]['fallback_check']['accepted'] is False
    assert fallback_events[-1]['fallback_check']['reason'] == 'fallback_reservation_evidence_changed'


@pytest.mark.parametrize('choice', ['reject', None])
def test_negative_request_is_bound_to_host_ledger_and_preserves_scope(item, choice):
    item.control.choice = choice
    assert not propose(item)
    verdict = verify(item)
    assert verdict['passed'] and verdict['request_binding_verified']
    assert verdict['negative_disposition_verified']
    assert not verdict['scope_amendment_applied'] and not verdict['reservation_observed']
    assert not verdict['host_signature_verified_by_worker']
    if choice is None:
        # Deadline checks precede IPC reads; a missing worker result is real.
        assert item.actor.approval_actor.host_result is None
        assert item.host.result['consumption']['reason'] == 'expired'


@pytest.mark.parametrize('choice', ['reject', None])
@pytest.mark.parametrize('missing', ['host', 'ledger', 'consumption'])
def test_negative_request_cannot_pass_without_completed_host_evidence(item, choice, missing):
    item.control.choice = choice
    assert not propose(item)
    host = item.host.record()
    ledger = item.ledger.status(item.actor.approval_actor.frame['request_id'])
    if missing == 'host':
        host = None
    elif missing == 'ledger':
        ledger = None
    else:
        ledger['consumption'] = None
    verdict = verify_inflight_return(study(item), expected_contract=item.contract,
        host_record=host, signed_ledger_record=ledger)
    assert not verdict['passed']


@pytest.mark.parametrize('choice', ['reject', None])
@pytest.mark.parametrize('mutation', [
    'host_context', 'host_bounds', 'frame_deadline', 'frame_candidate', 'frame_hash',
    'request_hash', 'receipt_hash', 'consumption_hash', 'consumption_scope',
    'request_snapshot_missing', 'request_event_missing', 'fallback_event_missing',
    'transient_scope_expansion', 'applied_reservation', 'worker_claims_crypto',
    'hidden_request', 'unauthorized_dispatch',
])
def test_negative_request_rejects_tampered_authority_evidence(item, choice, mutation):
    item.control.choice = choice
    assert not propose(item)
    value = study(item)
    record, host = value['inflight_approval'], item.host.record()
    worker = record['worker']
    frame = worker['request_frame']
    ledger = item.ledger.status(frame['request_id'])
    if mutation == 'host_context':
        host['context']['vehicle_id'] = 'other-ship'
    elif mutation == 'host_bounds':
        host['bounds']['wall_deadline_s'] += 1.
    elif mutation == 'frame_deadline':
        frame['simulation_deadline_s'] += 1.
    elif mutation == 'frame_candidate':
        frame['candidate_sha256'] = '0' * 64
    elif mutation == 'frame_hash':
        host['result']['request_frame_sha256'] = '0' * 64
    elif mutation in {'request_hash', 'receipt_hash', 'consumption_hash'}:
        ledger[mutation.removesuffix('_hash')]['sha256'] = '0' * 64
    elif mutation == 'consumption_scope':
        ledger['consumption']['scope']['commands'].append('select_next_orbit')
    elif mutation == 'request_snapshot_missing':
        record['snapshots'] = [s for s in record['snapshots'] if s['observation'] != frame['observation']]
    elif mutation == 'request_event_missing':
        worker['events'] = [e for e in worker['events'] if e['event'] != 'm3_scope_change_requested']
    elif mutation == 'fallback_event_missing':
        worker['events'] = [e for e in worker['events'] if e['event'] != 'm3_scope_update_not_applied']
    elif mutation == 'transient_scope_expansion':
        record['snapshots'][-1]['scope']['commands'].append('select_next_orbit')
        record['snapshots'][-1]['revision'] = 1
    elif mutation == 'applied_reservation':
        worker['applied_reservation'] = {'revision': 1}
    elif mutation == 'worker_claims_crypto':
        worker['host_signature_verified_by_worker'] = True
    elif mutation == 'hidden_request':
        worker['request_frame'] = worker['host_result'] = None
        worker['events'] = []
    else:
        value['dispatch'] = {'candidate_id': 'next_orbit', 'time_s': 1200.}
        value['execution']['events'].append({'event': 'm3_return_dispatch_authority_checked',
            'time_s': 1200., 'candidate_id': 'next_orbit', 'check': {'accepted': True,
            'scope_sha256': digest(item.contract['initial_scope']), 'scope_revision': 0,
            'candidate_sha256': digest(value['candidates']['next_orbit'])}})
    verdict = verify_inflight_return(value, expected_contract=item.contract,
        host_record=host, signed_ledger_record=ledger)
    assert not verdict['passed'], mutation


def test_expiry_cannot_be_claimed_before_original_deadline(item):
    item.control.choice = None
    assert not propose(item)
    value = study(item)
    event = next(e for e in value['inflight_approval']['worker']['events']
                 if e['event'] == 'm3_scope_update_not_applied')
    event['time_s'] = 1009.  # A real snapshot exists, but neither clock expired.
    verdict = verify(item, value)
    assert not verdict['passed'] and 'negative_expiry_before_original_deadline' in verdict['issues']


def test_rejection_requires_worker_to_observe_bound_host_result(item):
    item.control.choice = 'reject'
    assert not propose(item)
    value = study(item)
    value['inflight_approval']['worker']['host_result'] = None
    verdict = verify(item, value)
    assert not verdict['passed'] and 'negative_host_result_not_observed' in verdict['issues']


def test_approved_but_no_longer_feasible_request_has_no_applied_authority(item):
    item.control.mutate = lambda: item.eligible.update(value=False)
    assert not propose(item)
    verdict = verify(item)
    assert verdict['passed'] and verdict['negative_disposition_verified']
    assert not verdict['scope_amendment_applied'] and item.actor.scope_revision == 0


def test_no_request_is_not_misreported_as_verified_negative_disposition(item):
    verdict = verify(item)
    assert verdict['passed']
    assert not verdict['request_binding_verified'] and not verdict['negative_disposition_verified']


@pytest.mark.parametrize('mutation, expected_issue', [
    ('request_deadline', 'host_evidence_outside_original_contract'),
    ('request_observation', 'host_evidence_outside_original_contract'),
    ('receipt_authentication', 'host_receipt_disposition_binding'),
    ('receipt_choice', 'host_consumption_disposition_binding'),
    ('consumption_scope', 'host_consumption_disposition_binding'),
    ('future_observation', 'host_observation_clock_binding'),
])
def test_negative_verifier_checks_semantics_beyond_self_consistent_hashes(item, mutation, expected_issue):
    item.control.choice = 'reject'
    assert not propose(item)
    value, host = study(item), item.host.record()
    worker = value['inflight_approval']['worker']
    ledger = item.ledger.status(worker['request_frame']['request_id'])
    request, receipt, consumed = (ledger[k] for k in ('request', 'receipt', 'consumption'))
    if mutation == 'request_deadline':
        request['simulation_deadline_s'] += 1.
    elif mutation == 'request_observation':
        request['initial_observation']['state_sha256'] = '0' * 64
        request['observed_state_sha256'] = '0' * 64
    elif mutation == 'receipt_authentication':
        receipt['local_operator_authenticated'] = False
    elif mutation == 'receipt_choice':
        receipt['choice'] = 'approve'
    elif mutation == 'consumption_scope':
        consumed['scope']['commands'].append('select_next_orbit')
        consumed['scope_sha256'] = digest(consumed['scope'])
    else:
        consumed['latest_observation']['wall_time_s'] += 1.
    # Hash consistency is not authentication. Supply authentic signatures from
    # the fixture host to prove that outer-contract checks still reject it.
    request = item.ledger._sign({k: v for k, v in request.items() if k not in {'sha256', 'signature'}})
    receipt['request_sha256'] = request['sha256']
    receipt = item.ledger._sign({k: v for k, v in receipt.items() if k not in {'sha256', 'signature'}})
    consumed['request_sha256'], consumed['receipt_sha256'] = request['sha256'], receipt['sha256']
    consumed = item.ledger._sign({k: v for k, v in consumed.items() if k not in {'sha256', 'signature'}})
    ledger = {'request': request, 'receipt': receipt, 'consumption': consumed}
    host['request'] = host['result']['request'] = request
    host['result']['consumption'] = consumed
    worker['host_result'] = deepcopy(host['result'])
    verdict = verify_inflight_return(value, expected_contract=item.contract,
        host_record=host, signed_ledger_record=ledger)
    assert not verdict['passed'] and expected_issue in verdict['issues']

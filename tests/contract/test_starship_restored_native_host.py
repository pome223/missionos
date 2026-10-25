"""Real inherited-pipe host contracts with synthetic actual origin; no plant/AI."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from src.gateway.starship_inflight_approval import InflightApprovalService
from src.runtime import starship_restored_native_host as native
from src.runtime import starship_restored_contract as contract
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_approval import RestoredApprovalBinding, RestoredApprovalWorker
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy
from src.runtime.starship_restored_return import RestoredReturnRequest
from scripts.smoke_starship_restored_approval import Clock, synthetic_origin
from tests.contract.test_starship_restored_decision_pipe import request as decision_request


def fixture_judgment(request, provider):
    return {"schema":"missionos.starship_restored_judgment.v1", "request_id":request["request_id"],
        "request_sha256":native._hash(request), "provider":provider, "status":"valid",
        "action":"continue_deployment" if request["phase"] == "deployment" else "select_nominal",
        "model_inference_invoked":False, "execution_permission_granted":False}


class FixedAgent:
    def __init__(self, *, active, clock):
        self.active, self.clock = active, clock
    def assess(self, request, *, provider, **_kwargs):
        assert self.active()
        return fixture_judgment(request, provider)


@pytest.fixture(autouse=True)
def no_actual_plant_provider_or_external_calls(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_sixdof_mission as mission
    from src.runtime import starship_restored_return as returning
    from src.intelligence.starship_restored_agent import RestoredAgent
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Native-host contract tests forbid integration, live providers and network/process execution")
    for owner, name in ((dyn,"step"),(dyn,"_rk4"),(dyn,"_rhs"),(env,"step"),(mission,"advance_plant"),
        (returning,"advance_plant"),(returning,"forecast_restored_return"),(RestoredAgent,"assess"),
        (socket,"create_connection"),(subprocess,"Popen")):
        monkeypatch.setattr(owner,name,forbidden)


@pytest.fixture
def scenario(tmp_path):
    old, _ = synthetic_origin()
    source = deepcopy(old.record()["source_sha256"])
    source.update({"src/runtime/starship_restored_native_host.py":"2"*64,
        "src/runtime/starship_restored_coordinator.py":"3"*64,"src/runtime/starship_restored_integration.py":"4"*64})
    raw = json.loads(old.source_record_bytes())
    raw["source_sha256"] = source
    raw["experimental_mission_ascent"]["settings"]["ascent"]["shutdowns"] = [
        {"engine_name":"booster_main_32","time_s":76.},{"engine_name":"ship_main_3","time_s":437.}]
    for state in [raw["experimental_mission_ascent"]["cutoff_measurement"]["state"],
        raw["experimental_mission_ascent"]["settled_measurement"]["state"],raw["final_state"],
        *(item["state"] for item in raw["state_history"])]:
        state["engine_states"][3]["available"] = False
    encoded = native._wire(raw)
    profile_raw = (Path(native.__file__).resolve().parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(),encoded,profile_raw,source)
    clock, pairs = Clock(), [approval_pipe() for _ in range(7)]
    context, plan = {"run_id":"native-host","mission_id":"synthetic","vehicle_id":"ship"}, "a"*64
    directory = tmp_path/"governance"
    directory.mkdir()
    (directory/"closed-origin.json").write_bytes(origin.source_record_bytes())
    anchor = contract.RestoredCandidateSchedule(origin)
    initial = RestoredReturnRequest(origin,anchor,"nominal",profile_raw,source)
    binding = RestoredApprovalBinding(origin,context=context,plan_sha256=plan,expected_source_sha256=source,started_wall_s=clock())
    ready = {"schema":native.READY_SCHEMA,"context":context,"plan_sha256":plan,"source_sha256":native._hash(source),
        "profile_sha256":contract.PROFILE_SHA256,"origin_file":"closed-origin.json",
        "source_record_sha256":origin.source_record_sha256,"origin_sha256":native._hash(origin.record()),
        "binding_sha256":native._hash(binding.record()),"opportunity_anchor_sha256":native._hash(anchor.opportunity_anchor_record()),
        "initial_reservation_sha256":native._hash(initial.record()),"integration_policy_sha256":native._hash(RestoredIntegrationPolicy(2).record()),
        "started_wall_s":clock(),"nonce":"b"*64}
    (directory/"origin-ready.json").write_bytes(native._wire(ready))
    service = InflightApprovalService(clock=clock)
    kwargs = {"context":context,"plan_sha256":plan,"expected_source_sha256":source,"profile_raw_bytes":profile_raw,
        "integration_policy":RestoredIntegrationPolicy(2),"mode":"fixture","governance_dir":directory,
        "ledger_path":tmp_path/"host.sqlite","signing_key":b"synthetic-signing-key-at-least-32-bytes",
        "operator_token":"synthetic-local-operator-token-at-least-32chars","operator_id":"synthetic-operator",
        "decision_request_reader":pairs[0][0],"decision_response_writer":pairs[1][1],"ready_reader":pairs[2][0],
        "ack_writer":pairs[3][1],"approval_request_reader":pairs[4][0],"approval_grant_writer":pairs[5][1],
        "telemetry_reader":pairs[6][0],"service":service,"agent_factory":FixedAgent,"clock":clock}
    host = native.RestoredNativeHost(**kwargs)
    value = SimpleNamespace(host=host,kwargs=kwargs,pairs=pairs,ready=ready,origin=origin,initial=initial,
        binding=binding,directory=directory,clock=clock,service=service,source=source,profile=profile_raw,context=context,plan=plan)
    yield value
    host.close()
    for pair in pairs:
        for endpoint in pair:
            endpoint.close()


def telemetry(value, *, sequence=1,when=None,wall=None,revision=0,request=None):
    state = deepcopy(value.origin.record()["state"])
    if when is not None:
        state["time_s"] = when
    bound = value.binding.record()
    binding = value.initial.record() if request is None else request
    runtime = {"run_active":True,"simulation_time_s":state["time_s"],"restored_origin_sha256":bound["restored_origin_sha256"],
        "revision":revision,"scope":bound["initial_scope"] if revision == 0 else bound["registry"],
        "reservation_sha256":native._hash(binding),"observation":{"state_sha256":native._hash(state),
            "simulation_time_s":state["time_s"],"wall_time_s":value.clock() if wall is None else wall}}
    return {"schema":native.TELEMETRY_SCHEMA,"context":value.context,"plan_sha256":value.plan,
        "source_sha256":native._hash(value.source),"sequence":sequence,"runtime_state":runtime,"actual_state":state,
        "phase":"orbital_coast","vehicle_sha256":native._hash(value.origin.record()["vehicle"]),
        "binding_sha256":native._hash(binding),"return_controller_started":False,
        "integration_policy_sha256":native._hash(RestoredIntegrationPolicy(2).record()),"state_assigned":False}


def establish(value):
    value.pairs[2][1].send(value.ready)
    value.pairs[6][1].send(telemetry(value))
    value.host.tick()
    ack = value.pairs[3][0].poll()
    assert ack["ledger_ready"] and ack["ready_sha256"] == native._hash(value.ready)
    return ack


def test_host_policy_is_explicit_source_bound_and_not_approval(scenario):
    from src.runtime import starship_restored_return_review as review
    policy = deepcopy(review.DEFAULT_POLICY)
    source = dict(scenario.source)
    source['src/runtime/starship_restored_return_review.py'] = sha256(Path(review.__file__).read_bytes()).hexdigest()
    kwargs = scenario.kwargs | {'expected_source_sha256': source,
        'ledger_path': scenario.directory.parent/'review-host.sqlite', 'return_review_policy': policy}
    host = native.RestoredNativeHost(**kwargs)
    try:
        record = host.record()
        assert record['return_review_policy'] == record['decision_host']['return_review_policy'] == policy
        assert record['return_review_policy_sha256'] == record['decision_host']['return_review_policy_sha256'] == review.policy_digest(policy)
        assert record['approval_host'] is None and record['operator_ledger'] == []
        assert not Path(kwargs['ledger_path']).exists()
        assert 'return_review_policy' not in scenario.host.record()
    finally:
        host.close()


def test_host_missing_review_source_cannot_create_a_policy_or_operator_ledger(scenario):
    from src.runtime.starship_restored_return_review import DEFAULT_POLICY
    kwargs = scenario.kwargs | {'ledger_path': scenario.directory.parent/'missing-policy.sqlite',
        'return_review_policy': DEFAULT_POLICY}
    with pytest.raises(ValueError, match='return_review_source_opt_in'):
        native.RestoredNativeHost(**kwargs)
    assert not Path(kwargs['ledger_path']).exists()


def test_constructor_does_not_create_ledger_or_ack_before_ready_actual_telemetry(scenario):
    s = scenario
    assert not Path(s.kwargs["ledger_path"]).exists() and s.host.origin is None
    s.host.tick()
    s.pairs[2][1].send(s.ready)
    s.host.tick()
    assert s.host.origin.record() == s.origin.record() and s.host.approval_host is None
    assert s.pairs[3][0].poll() is None and not Path(s.kwargs["ledger_path"]).exists()
    s.pairs[6][1].send(telemetry(s))
    s.host.tick()
    assert s.pairs[3][0].poll()["ledger_ready"] and Path(s.kwargs["ledger_path"]).exists()


def test_exact_original_worker_triple_kept_and_telemetry_drained_before_approval(scenario):
    s = scenario
    establish(s)
    worker = RestoredApprovalWorker(s.binding,initial_reservation_sha256=native._hash(s.initial.record()),
        request_writer=s.pairs[4][1],grant_reader=s.pairs[5][0],admission=lambda *_a:{"accepted":True},command=lambda *_a:None,clock=s.clock)
    original = telemetry(s,sequence=2,when=s.origin.time_s+.25,wall=s.clock())
    s.clock.now += .5
    s.pairs[6][1].send(original)
    worker.request_change(request_id="change1",candidate_sha256="c"*64,state=original["runtime_state"])
    s.host.tick()
    assert s.host.approval_host.bridge.request is not None
    history = s.host.snapshot()["known_observations"]
    assert history[-1] == original["runtime_state"]["observation"]
    recorded = s.host.record()["telemetry"][-1]
    assert recorded["received_wall_s"] == 1000.5 and recorded["frame"]["runtime_state"]["observation"]["wall_time_s"] == 1000.
    assert s.host.approval_host.bridge.request["observed_state_sha256"] == history[-1]["state_sha256"]


@pytest.mark.parametrize("field",["nonce","source_record_sha256","origin_sha256","binding_sha256","initial_reservation_sha256",
    "opportunity_anchor_sha256","integration_policy_sha256","source_sha256","plan_sha256","profile_sha256"])
def test_forged_ready_never_creates_ledger_or_ack(scenario,field):
    s = scenario
    ready = deepcopy(s.ready)
    ready[field] = "f"*64
    s.pairs[2][1].send(ready)
    with pytest.raises((ValueError,OSError)):
        s.host.tick()
    assert not Path(s.kwargs["ledger_path"]).exists() and s.pairs[3][0].poll() is None


def test_ready_replay_rejected_after_single_ledger(scenario):
    s = scenario
    establish(s)
    s.pairs[2][1].send(s.ready)
    with pytest.raises(ValueError,match="replayed_ready"):
        s.host.tick()
    assert len(s.host.record()["events"]) == 1


@pytest.mark.parametrize("mutation",["statehash","wall_future","wall_stale","sequence","scope","source","policy","vehicle","binding","availability_reset","fuel_added","first_state","state_assigned"])
def test_spoofed_initial_telemetry_rejects_before_any_ledger(scenario,mutation):
    s = scenario
    frame = telemetry(s)
    if mutation == "statehash":
        frame["runtime_state"]["observation"]["state_sha256"] = "f"*64
    elif mutation in ("wall_future","wall_stale"):
        frame["runtime_state"]["observation"]["wall_time_s"] += 1. if mutation == "wall_future" else -6.
    elif mutation == "sequence":
        frame["sequence"] = True
    elif mutation == "scope":
        frame["runtime_state"]["scope"] = s.binding.record()["registry"]
    elif mutation in ("source","policy","vehicle","binding"):
        frame[{"source":"source_sha256","policy":"integration_policy_sha256","vehicle":"vehicle_sha256","binding":"binding_sha256"}[mutation]] = "f"*64
    elif mutation == "state_assigned":
        frame["state_assigned"] = True
    else:
        if mutation == "availability_reset":
            frame["actual_state"]["engine_states"][3]["available"] = True
        elif mutation == "fuel_added":
            frame["actual_state"]["propellant_kg"] += 1.
        else:
            frame["actual_state"]["r_eci_m"][0] += 1.
        frame["runtime_state"]["observation"]["state_sha256"] = native._hash(frame["actual_state"])
    s.pairs[2][1].send(s.ready)
    s.pairs[6][1].send(frame)
    with pytest.raises((ValueError,TypeError)):
        s.host.tick()
    assert not Path(s.kwargs["ledger_path"]).exists() and s.pairs[3][0].poll() is None


def test_real_decision_pipes_before_ready_use_fixture_factory_and_keep_false_inference(scenario):
    s = scenario
    client = RestoredDecisionClient(context=s.context,plan_sha256=s.plan,source_sha256=native._hash(s.source),
        request_writer=s.pairs[0][1],response_reader=s.pairs[1][0],clock=s.clock)
    request = decision_request()
    client.begin(request)
    result = None
    for _ in range(1000):
        s.host.tick()
        result = client.poll(request["time_s"])
        if result is not None:
            break
        threading.Event().wait(.001)
    assert result["status"] == "valid" and result["model_inference_invoked"] is False
    assert s.host.record()["decision_host"]["requests"][0]["attempts"][0]["judgment"]["model_inference_invoked"] is False
    assert s.host.approval_host is None


def test_mode_cannot_smuggle_fixture_factory_into_live_or_live_default_into_fixture(scenario):
    for changed in ({"mode":"live"},{"agent_factory":None},{"mode":"unknown"}):
        with pytest.raises(ValueError,match="mode|policy"):
            native.RestoredNativeHost(**(scenario.kwargs|changed))


def test_host_record_has_no_signing_or_bearer_values_and_no_automatic_operator(scenario):
    s = scenario
    establish(s)
    record = s.host.record()
    assert record["mode"] == "fixture" and record["operator_ledger"] == []
    raw = native._wire(record)
    assert s.kwargs["operator_token"].encode() not in raw and s.kwargs["signing_key"] not in raw
    assert record["handshake"]["ready"] == s.ready and record["approval_host"]["model_calls"] == 0
    s.host.close()
    assert s.host.snapshot()["run_active"] is False


@pytest.mark.parametrize("bad",["../escape","/absolute","a/../escape","./closed-origin.json","a\\b"])
def test_artifact_path_traversal_rejected_before_read(tmp_path,bad):
    with pytest.raises(ValueError,match="unsafe_artifact_path"):
        native.read_origin_artifact(tmp_path,bad,expected_sha256="a"*64)


def test_regular_artifact_hash_size_symlink_and_symlink_ancestor_fail_closed(tmp_path):
    actual = tmp_path/"actual"
    actual.mkdir()
    raw = b'{"synthetic":true}'
    (actual/"origin.json").write_bytes(raw)
    token = sha256(raw).hexdigest()
    assert native.read_origin_artifact(actual,"origin.json",expected_sha256=token) == raw
    with pytest.raises(ValueError,match="hash_mismatch"):
        native.read_origin_artifact(actual,"origin.json",expected_sha256="0"*64)
    with pytest.raises(ValueError,match="size_or_type"):
        native.read_origin_artifact(actual,"origin.json",expected_sha256=token,maximum_bytes=2)
    (tmp_path/"link").symlink_to(actual,target_is_directory=True)
    (actual/"file-link").symlink_to(actual/"origin.json")
    for directory,name in ((actual,"file-link"),(tmp_path/"link","origin.json")):
        with pytest.raises(OSError):
            native.read_origin_artifact(directory,name,expected_sha256=token)


def test_telemetry_cross_pipe_arrival_before_ready_poll_is_buffered_not_rejected(scenario):
    s = scenario
    s.pairs[6][1].send(telemetry(s))
    s.host.tick()
    assert s.host.origin is None and not s.host.failed and s.host.record()["telemetry"] == []
    s.pairs[2][1].send(s.ready)
    s.host.tick()
    assert len(s.host.record()["telemetry"]) == 1 and s.pairs[3][0].poll()["ledger_ready"]


def test_artifact_root_with_symlink_ancestor_is_rejected(tmp_path):
    real = tmp_path/"real"
    (real/"child").mkdir(parents=True)
    (real/"child/origin.json").write_bytes(b"{}")
    (tmp_path/"ancestor-link").symlink_to(real,target_is_directory=True)
    with pytest.raises(OSError):
        native.read_origin_artifact(tmp_path/"ancestor-link/child","origin.json",expected_sha256=sha256(b"{}").hexdigest())

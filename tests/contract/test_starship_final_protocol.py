"""Actual inherited-pipe exchange; no provider, flight or network execution."""
from copy import deepcopy
import threading

import pytest

from src.runtime import starship_final_protocol as rpc
from src.runtime.starship_inflight_runtime import approval_pipe


@pytest.fixture
def endpoints():
    hr, cw = approval_pipe()
    cr, hw = approval_pipe()
    now, calls = [100.], []

    def build(handler=None):
        def handle(value, **kwargs):
            calls.append((deepcopy(value), kwargs))
            return {"status": "fixture_only", "action": "observe", "model_inference_invoked": False}
        binding = {"plan_sha256": "a"*64, "source_sha256": "b"*64, "clock": lambda: now[0]}
        return (rpc.FinalObservationClient(cw, cr, **binding),
            rpc.FinalObservationHost(hr, hw, handler=handler or handle, **binding))
    yield build, now, calls, hw
    for end in hr, cw, cr, hw:
        end.close()


def request(name="ascent-1", domain="ascent"):
    return rpc.request(name, domain, 10., 45., observation(domain))


def observation(domain="ascent"):
    return {"schema": "missionos.starship_final_supervision_observation.v1", "scope_sha256": "c"*64,
        "facts": {"altitude_m": 200.}, "allowed_actions": ["continue_mission", "withhold_deployment"]
            if domain == "ascent" else ["splashdown", "inhibit_recovery"]}


def pump(client, host, sim=10.):
    for _ in range(1000):
        host.tick()
        result = client.poll(sim)
        if result is not None:
            return result
        threading.Event().wait(.001)
    pytest.fail("Fixture handler did not complete")


def test_original_wall_epoch_and_real_pipe_binding(endpoints):
    build, now, calls, _ = endpoints
    client, host = build()
    now[0] = 110.
    frame = client.begin(request(), started_wall_s=100.)
    assert calls == [] and frame["wall_deadline_s"] == 135.
    result = pump(client, host)
    assert result["model_inference_invoked"] is False
    assert calls[0][1] == {"remaining_s": 25., "outer_deadline_monotonic": 135.}
    assert client.record()["provider_calls_by_worker"] == 0
    assert host.record()["execution_permission_granted"] is False


@pytest.mark.parametrize("start", [True, -1, 111., float("nan"), float("inf"), "100"])
def test_bad_epoch_never_sends(endpoints, start):
    build, now, calls, _ = endpoints
    client, host = build()
    now[0] = 110.
    with pytest.raises(ValueError):
        client.begin(request(), started_wall_s=start)
    assert client.sequence == 0 and not client.seen and calls == []
    assert host.reader.poll() is None


@pytest.mark.parametrize("private", ["seed", "truth", "physical_factor", "fault_name", "api_key", "secret_token"])
def test_private_truth_rejected_before_pipe(private):
    with pytest.raises(ValueError, match="private_truth"):
        rpc.request("test", "ascent", 10., 45., {"nested": {private: .8}})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), "/Users/private/input"])
def test_unbounded_or_nonfinite_observations_rejected(value):
    with pytest.raises(ValueError):
        rpc.request("test", "ascent", 10., 45., {"measurement": value})


def test_error_is_type_only_no_retry(endpoints):
    build, _, _, _ = endpoints
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("credential-like private text must never escape")
    client, host = build(fail)
    client.begin(request())
    assert pump(client, host) == {"status": "unavailable", "action": None}
    assert attempts == [1]
    assert host.record()["sessions"][0]["answer"]["error_type"] == "RuntimeError"
    assert "credential-like" not in str(host.record())


def test_expiry_retains_authenticated_late_result_without_adoption(endpoints):
    build, now, _, _ = endpoints
    client, host = build()
    client.begin(request())
    assert client.poll(45.) == {"status": "expired", "action": None}
    # Host completes the old request later while wall time still allows it.
    for _ in range(1000):
        host.tick()
        if host.pending is None and host.sessions:
            break
        threading.Event().wait(.001)
    assert client.poll(46.) is None
    assert client.events[0]["adopted"] is False
    assert client.completed[0]["delivered_result"] is None
    now[0] = 101.
    client.begin(rpc.request("booster-2", "booster", 46., 81., observation("booster")))
    assert pump(client, host, 46.)["status"] == "fixture_only"


def test_tampered_binding_is_rejected(endpoints):
    build, _, _, hw = endpoints
    client, _ = build()
    sent = client.begin(request())
    result = {"status": "fixture_only", "action": "observe"}
    hw.send({"schema": rpc.RESPONSE, "plan_sha256": "f"*64, "source_sha256": "b"*64,
        "sequence": 1, "request_sha256": sent["request_sha256"], "result": result,
        "result_sha256": rpc.digest(result)})
    with pytest.raises(ValueError, match="response_binding"):
        client.poll(10.)


def test_valid_completed_response_cannot_be_replayed(endpoints):
    build, _, _, hw = endpoints
    client, host = build()
    client.begin(request())
    pump(client, host)
    hw.send(client.completed[0]["response"])
    with pytest.raises(ValueError, match="replayed_response"):
        client.poll(11.)


def test_one_pending_request_and_spent_id_cannot_be_retried(endpoints):
    build, _, _, _ = endpoints
    client, host = build()
    client.begin(request())
    with pytest.raises(ValueError):
        client.begin(request("another"))
    pump(client, host)
    with pytest.raises(ValueError):
        client.begin(request())


def test_simulation_clock_must_not_regress(endpoints):
    build, _, _, _ = endpoints
    client, _ = build()
    client.begin(request())
    assert client.poll(11.) is None
    with pytest.raises(ValueError, match="regressed"):
        client.poll(10.)


@pytest.mark.parametrize("domain", [[], None, "unknown", "sensor"])
def test_unknown_domain_is_not_an_extra_capability(domain):
    with pytest.raises(ValueError):
        rpc.request("test", domain, 10., 45., {})


@pytest.mark.parametrize("name,value", [
    ("actual_engine_limits_n", [1., 2., 3.]),
    ("actual_vehicle", {"engines": [{"max_thrust_n": 12345.}]}),
    ("controller", {"phase": "ballistic_return"}),
    ("note", "Numerical source is /Users/private/flight.json"),
])
def test_nonpublic_numerical_capture_never_reaches_handler(endpoints, name, value):
    build, _, calls, _ = endpoints
    client, host = build()
    public = observation()
    public["facts"][name] = value
    with pytest.raises(ValueError):
        client.begin(rpc.request("hidden-capture", "ascent", 10., 45., public))
    assert host.reader.poll() is None and calls == [] and client.sequence == 0


def test_handler_finishing_after_wall_timeout_is_retained_but_not_adopted(endpoints):
    build, now, _, _ = endpoints
    entered, release = threading.Event(), threading.Event()
    def handler(*args, **kwargs):
        entered.set()
        assert release.wait(2.)
        return {"status": "fixture_only", "action": "observe"}
    client, host = build(handler)
    client.begin(request())
    host.tick()
    assert entered.wait(1.)
    now[0] = 136.
    host.tick()
    assert client.poll(46.) == {"status": "expired", "action": None}
    assert host.record()["sessions"][0]["handler_finished"] is False
    release.set()
    host.sessions[0]["thread"].join(timeout=1.)
    saved = host.record()["sessions"][0]
    assert saved["answer"]["result"]["status"] == "fixture_only"
    assert saved["delivered_result"]["status"] == "expired"
    assert saved["late_answer_adopted"] is False and saved["handler_finished"] is True


def test_same_actual_visual_projection_can_cross_the_pipe_without_pixels(tmp_path):
    from tests.contract.test_starship_final_visual import episode
    observer, _, _ = episode(tmp_path)
    envelope = observer.requests[0]
    row = rpc.request("visual-pipe-1", "visual", observer.record()["episode"]["started_s"],
        envelope["request"]["deadline_s"], envelope)
    assert row["observation"]["frame_refs"]
    assert b"data:image" not in rpc.wire(row) and len(rpc.wire(row)) < rpc.MAX_FRAME_BYTES
    for section in ("telemetry", "prior_diagnosis"):
        changed = deepcopy(envelope)
        hidden = {"actual_engine_limits_n": [12345., 23456.]}
        if section == "telemetry":
            changed["request"][section].update(hidden)
        else:
            hidden.update(schema="missionos.starship_visual_diagnosis.v1", snapshot_time_s=128.)
            hidden["diagnosis_id"] = rpc.digest(hidden)
            changed["request"][section] = hidden
        changed["request_sha256"] = rpc.digest(changed["request"])
        changed["envelope_sha256"] = rpc.digest({k: v for k, v in changed.items() if k != "envelope_sha256"})
        with pytest.raises(ValueError):
            rpc.request("visual-hidden", "visual", 128., changed["request"]["deadline_s"], changed)

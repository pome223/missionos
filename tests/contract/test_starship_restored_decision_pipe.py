"""Inherited synthetic pipes and fake async agents; no model or plant calls."""
from copy import deepcopy
import socket
import subprocess
import threading

import pytest

from src.runtime import starship_restored_decision_pipe as pipe
from src.runtime.starship_inflight_runtime import approval_pipe, MAX_FRAME_BYTES

CONTEXT = {"run_id": "run-fixture", "mission_id": "mission-fixture", "vehicle_id": "ship"}


def request(*, phase="return", request_id="decision-1"):
    return {"schema": "missionos.starship_restored_decision_request.v1", "request_id": request_id,
        "phase": phase, "scope_sha256": "a"*64, "time_s": 2000.,
        "deadline_s": 2035. if phase == "deployment" else 2180.,
        "facts": {"time_s": 2000., "released_count": 13, "retained_count": 13,
            "candidate.nominal.contact_speed_mps": 4.7}, "notice_summary": "Synthetic operations notice.",
        "allowed_actions": ["continue_deployment", "withhold_deployment"] if phase == "deployment" else
            ["select_nominal", "select_next_orbit", "request_deep_reasoning"]}


def judgment(req, provider, action="select_nominal", *, valid=True):
    return {"schema": "missionos.starship_restored_judgment.v1", "request_id": req["request_id"],
        "request_sha256": pipe._digest(req), "provider": provider, "action": action if valid else None,
        "status": "valid" if valid else "no_valid_judgment", "summary": "Synthetic response.",
        "provider_receipt": {"status": "synthetic_decoder_only", "validation_error": None if valid else "model_mismatch"},
        "model_inference_invoked": False, "execution_permission_granted": False}


@pytest.fixture(autouse=True)
def no_model_network_or_physics(monkeypatch):
    from src.intelligence.starship_restored_agent import RestoredAgent
    from src.runtime import starship_sixdof as dyn

    def forbidden(*_a, **_kw):
        raise AssertionError("Synthetic decision transport tests cannot invoke models or physics")

    for owner, name in ((RestoredAgent, "assess"), (dyn, "step"), (dyn, "_rk4"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def endpoints():
    host_reader, client_writer = approval_pipe()
    client_reader, host_writer = approval_pipe()
    value = {"context": CONTEXT, "plan_sha256": "b"*64, "source_sha256": "c"*64}
    now, active, calls = [100.], [True], []

    def build(assess):
        class Agent:
            def __init__(self, *, active, clock):
                self.active, self.clock = active, clock

            def assess(self, req, **kwargs):
                calls.append((deepcopy(req), dict(kwargs)))
                assert self.active() is True
                return assess(req, **kwargs)

        client = pipe.RestoredDecisionClient(**value, request_writer=client_writer,
            response_reader=client_reader, clock=lambda: now[0])
        host = pipe.RestoredDecisionHost(**value, reader=host_reader, writer=host_writer,
            agent_factory=Agent, active=lambda: active[0], clock=lambda: now[0])
        return client, host

    yield {"build": build, "now": now, "active": active, "calls": calls,
        "client_writer": client_writer, "host_writer": host_writer, "bindings": value}
    for endpoint in (host_reader, client_writer, client_reader, host_writer):
        endpoint.close()


def pump(client, host, sim_s=2000.):
    for _ in range(2000):
        host.tick()
        result = client.poll(sim_s)
        if result is not None:
            return result
        threading.Event().wait(.001)
    pytest.fail("Synthetic agent failed to finish")


def test_worker_nonblocking_begin_never_instantiates_or_invokes_agent(endpoints):
    client, _host = endpoints["build"](lambda *_a, **_kw: pytest.fail("host not ticked"))
    frame = client.begin(request())
    assert client.poll(2000.) is None and endpoints["calls"] == []
    assert frame["sequence"] == 1 and frame["wall_deadline_s"] == 280.
    assert client.record()["provider_calls_by_worker"] == 0


def test_request_preparation_consumes_original_wall_epoch_in_host_frame(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider))
    endpoints["now"][0] = 120.
    frame = client.begin(request(), started_wall_s=100.)
    result = pump(client, host)
    assert result["status"] == "valid"
    assert frame["started_wall_s"] == 100. and frame["wall_deadline_s"] == 280.
    assert frame["wall_deadline_s"] == frame["started_wall_s"]+request()["deadline_s"]-request()["time_s"]
    _, kwargs = endpoints["calls"][0]
    assert kwargs["remaining_s"] == 160. and kwargs["outer_deadline_monotonic"] == 280.
    assert host.record()["requests"][0]["frame"] == frame


@pytest.mark.parametrize("now,started", [
    (100., -1.), (100., 101.), (100., True), (100., "100"),
    (100., float("nan")), (100., float("inf")), (280., 100.), (300., 100.),
])
def test_invalid_or_expired_original_wall_epoch_has_no_send_or_identity_side_effects(endpoints, now, started):
    client, host = endpoints["build"](lambda *_a, **_kw: pytest.fail("host must not be invoked"))
    endpoints["now"][0] = now
    with pytest.raises(ValueError, match="original_wall_epoch_invalid_or_expired"):
        client.begin(request(), started_wall_s=started)
    assert client.sequence == 0 and client.pending is None and not client.seen and not client.events
    assert host.reader.poll() is None and not endpoints["calls"]


def test_valid_jev_choice_is_preserved_and_is_not_an_approval(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider, "select_nominal"))
    client.begin(request())
    result = pump(client, host)
    assert result["status"] == "valid" and result["action"] == "select_nominal"
    assert result["execution_permission_granted"] is result["delegation_is_approval"] is False
    assert result["model_inference_invoked"] is False and result["synthetic_transport_is_model_evidence"] is False
    assert len(endpoints["calls"]) == len(result["attempts"]) == 1
    assert host.record()["requests"][0]["started_attempts"][0]["provider"] == "typesafe"


def test_deployment_setup_overhead_uses_original_wall_deadline(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider, "continue_deployment"))
    frame = client.begin(request(phase="deployment"))
    endpoints["now"][0] += .1
    result = pump(client, host)
    _, kwargs = endpoints["calls"][0]
    assert result["action"] == "continue_deployment" and kwargs["remaining_s"] == pytest.approx(34.9)
    assert kwargs["outer_deadline_monotonic"] == frame["wall_deadline_s"] == 135.


def test_explicit_delegation_preserves_jev_receipt_same_facts_and_fixed_epoch(endpoints):
    original = request()

    def assess(req, *, provider, remaining_s, outer_deadline_monotonic):
        assert outer_deadline_monotonic == 280.
        if provider == "typesafe":
            endpoints["now"][0] += 20.
            return judgment(req, provider, "request_deep_reasoning")
        assert remaining_s == 160. and req["deadline_s"] == 2160.
        assert req["facts"] == original["facts"] and req["scope_sha256"] == original["scope_sha256"]
        assert req["notice_summary"] == original["notice_summary"]
        assert req["allowed_actions"] == ["select_nominal", "select_next_orbit"]
        return judgment(req, provider, "select_next_orbit")

    client, host = endpoints["build"](assess)
    frame = client.begin(original)
    result = pump(client, host, 2020.)
    assert result["status"] == "valid" and result["action"] == "select_next_orbit"
    assert [row["provider"] for row in result["attempts"]] == ["typesafe", "deepseek"]
    assert result["attempts"][0]["judgment"]["action"] == "request_deep_reasoning"
    assert result["judgment"]["request_sha256"] != frame["request_sha256"]
    assert host.record()["requests"][0]["frame"]["wall_deadline_s"] == 280.


def test_deepseek_is_not_called_unless_150_seconds_remain(endpoints):
    def assess(req, provider, **_kwargs):
        endpoints["now"][0] += 31.
        return judgment(req, provider, "request_deep_reasoning")

    client, host = endpoints["build"](assess)
    client.begin(request())
    result = pump(client, host, 2031.)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["reason"] == "insufficient_deep_reasoning_window"
    assert len(endpoints["calls"]) == len(result["attempts"]) == 1
    assert result["judgment"]["action"] == "request_deep_reasoning"


def test_bad_jev_receipt_is_retained_without_deepseek_or_retry(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider, valid=False))
    client.begin(request())
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and len(endpoints["calls"]) == 1
    assert result["attempts"][0]["judgment"]["provider_receipt"]["validation_error"] == "model_mismatch"


def test_agent_exception_records_only_error_type_and_never_retries(endpoints):
    def failure(*_a, **_kw):
        raise RuntimeError("private exception content must not be recorded")

    client, host = endpoints["build"](failure)
    client.begin(request())
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and len(endpoints["calls"]) == 1
    assert result["attempts"][0]["error_type"] == "RuntimeError" and result["model_inference_invoked"] is None
    assert "private exception" not in str(result)


def test_one_thread_pending_is_nonblocking_and_timeout_retains_late_receipt(endpoints):
    entered, release = threading.Event(), threading.Event()

    def delayed(req, provider, **_kw):
        entered.set()
        assert release.wait(2.)
        return judgment(req, provider)

    client, host = endpoints["build"](delayed)
    client.begin(request())
    host.tick()
    assert entered.wait(1.)
    for _ in range(10):
        assert host.tick() is None and client.poll(2000.) is None
    assert len(endpoints["calls"]) == 1 and len(host.sessions) == 1
    endpoints["now"][0] = 280.
    host.tick()
    result = client.poll(2180.)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["model_inference_invoked"] is None
    release.set()
    host.sessions[0]["thread"].join(1.)
    report = host.record()
    assert report["requests"][0]["attempts"][0]["judgment"]["action"] == "select_nominal"
    assert report["requests"][0]["delivered_result"]["status"] == "no_valid_judgment"


def test_completed_answer_is_not_delivered_if_run_became_inactive(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider))
    client.begin(request())
    host.tick()
    host.sessions[0]["thread"].join(1.)
    endpoints["active"][0] = False
    result = pump(client, host)
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["attempts"][0]["judgment"]["status"] == "valid"


def test_worker_simulation_deadline_does_not_depend_on_host_wall_clock(endpoints):
    client, _host = endpoints["build"](lambda *_a, **_kw: pytest.fail("no host invocation"))
    client.begin(request())
    result = client.poll(2180.)
    assert result["reason"] == "original_deadline_expired" and result["action"] is None
    assert result["model_inference_invoked"] is None and endpoints["calls"] == []


def test_request_ids_are_single_use_and_pending_cannot_be_overwritten(endpoints):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider))
    client.begin(request())
    with pytest.raises(ValueError, match="duplicate_or_pending"):
        client.begin(request(request_id="another"))
    pump(client, host)
    with pytest.raises(ValueError, match="duplicate_or_pending"):
        client.begin(request())
    client.begin(request(request_id="new-decision"))
    pump(client, host)
    assert len(endpoints["calls"]) == 2 and len(host.sessions) == 2


@pytest.mark.parametrize("mutation", ["context", "source", "plan", "sequence", "hash", "duplicate"])
def test_host_rejects_mismatch_or_replay_before_any_new_attempt(endpoints, mutation):
    client, host = endpoints["build"](lambda req, provider, **_kw: judgment(req, provider))
    frame = client.begin(request())
    # Consume the original request without ticking the host, then inject the
    # sole mutated frame through the real inherited pipe.
    assert host.reader.poll() == frame
    if mutation == "context":
        frame["context"]["run_id"] = "other"
    elif mutation == "source":
        frame["source_sha256"] = "f"*64
    elif mutation == "plan":
        frame["plan_sha256"] = "f"*64
    elif mutation == "sequence":
        frame["sequence"] = True
    elif mutation == "hash":
        frame["request_sha256"] = "f"*64
    else:
        host.seen.add(frame["request_id"])
    endpoints["client_writer"].send(frame)
    with pytest.raises(ValueError):
        host.tick()
    assert endpoints["calls"] == [] and host.sessions == []
    assert host.record()["events"][-1]["event"] == "request_rejected"


def response_frame(begin):
    req = begin["request"]
    result = pipe._result(req, [{"provider": "typesafe", "remaining_s": 180.,
        "decision_deadline_s": req["deadline_s"], "request_sha256": pipe._digest(req),
        "judgment": judgment(req, "typesafe")}], judgment(req, "typesafe"))
    return {"schema": pipe.RESPONSE_FRAME,
        **{key: begin[key] for key in ("context", "plan_sha256", "source_sha256", "sequence", "request_id", "request_sha256")},
        "result": result, "result_sha256": pipe._digest(result)}


def test_late_retired_answer_cannot_become_the_next_phase_judgment(endpoints):
    client, _ = endpoints["build"](lambda *_a, **_kw: pytest.fail("no model"))
    first = client.begin(request())
    assert client.poll(2180.)["status"] == "no_valid_judgment"
    second_request = request(request_id="return-after-timeout")
    second_request.update(time_s=2200., deadline_s=2380.)
    second_request["facts"]["time_s"] = 2200.
    second = client.begin(second_request)
    late = response_frame(first)
    endpoints["host_writer"].send(late)
    assert client.poll(2200.) is None
    assert client.pending["frame"]["request_id"] == "return-after-timeout"
    assert client.record()["events"][-1]["event"] == "retired_late_response"
    assert client.record()["events"][-1]["adopted"] is False
    endpoints["host_writer"].send(response_frame(second))
    assert client.poll(2200.)["request_id"] == "return-after-timeout"
    endpoints["host_writer"].send(late)
    with pytest.raises(ValueError, match="replayed_response"):
        client.poll(2200.)


@pytest.mark.parametrize("mutation", ["context", "source", "plan", "sequence", "hash", "permission", "action", "attempt_hash"])
def test_worker_rejects_unbound_or_forged_judgment(endpoints, mutation):
    client, _host = endpoints["build"](lambda *_a, **_kw: pytest.fail("host not used"))
    frame = response_frame(client.begin(request()))
    if mutation == "context":
        frame["context"]["run_id"] = "other"
    elif mutation == "source":
        frame["source_sha256"] = "f"*64
    elif mutation == "plan":
        frame["plan_sha256"] = "f"*64
    elif mutation == "sequence":
        frame["sequence"] = True
    elif mutation == "hash":
        frame["request_sha256"] = "f"*64
    elif mutation == "permission":
        frame["result"]["execution_permission_granted"] = True
    elif mutation == "action":
        frame["result"]["action"] = "select_next_orbit"
    else:
        frame["result"]["attempts"][0]["request_sha256"] = "f"*64
    frame["result_sha256"] = pipe._digest(frame["result"])
    endpoints["host_writer"].send(frame)
    with pytest.raises(ValueError):
        client.poll(2000.)


def test_request_frame_size_guard_uses_existing_bounded_pipe(endpoints, monkeypatch):
    client, _host = endpoints["build"](lambda *_a, **_kw: pytest.fail("host not used"))
    monkeypatch.setattr(pipe, "MAX_FRAME_BYTES", 100)
    with pytest.raises(ValueError, match="frame_too_large"):
        client.begin(request())
    assert client.pending is None and endpoints["calls"] == []
    assert MAX_FRAME_BYTES == 16384


def test_clock_regression_and_unsolicited_response_fail_closed(endpoints):
    client, _host = endpoints["build"](lambda *_a, **_kw: pytest.fail("host not used"))
    begin = client.begin(request())
    assert client.poll(2001.) is None
    with pytest.raises(ValueError, match="clock_regressed"):
        client.poll(2000.)
    endpoints["host_writer"].send(response_frame(begin))
    assert client.poll(2001.)["status"] == "valid"
    endpoints["host_writer"].send(response_frame(begin))
    with pytest.raises(ValueError, match="unsolicited_or_replayed"):
        client.poll(2001.)

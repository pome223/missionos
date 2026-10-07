"""Executable mailbox limits and separation of command receipt from effect."""
from __future__ import annotations

import json

import pytest

from src.runtime.starship_flight_supervision import FlightSupervision


def telemetry(t, **overrides):
    return {"time_s": float(t), "phase": "orbital_coast", "payload_released_count": 0,
            "release_attempt_count": 1, "release_acknowledged": True, "sequencer_state": "inhibited",
            "perigee_altitude_m": 225000., "dynamic_pressure_pa": 0., "body_rate_rad_s": .001,
            "propellant_kg": 50000., "return_deadline_s": 500., **overrides}


def step(supervisor, t, **overrides):
    return supervisor.step(telemetry(t, **overrides), return_reserve_kg=28000., payload_interval_s=15.)


def pending(tmp_path):
    supervisor = FlightSupervision(tmp_path, "test-exchange")
    first, command = step(supervisor, 100)
    assert command is None and first["sequence"] == 1
    assert not (tmp_path / "request.json").exists()
    assert step(supervisor, 101) == (None, None)
    second, command = step(supervisor, 102)
    assert command is None and second["sequence"] == 2
    return supervisor


def response(supervisor, **overrides):
    return {"request_id": supervisor.log["request_id"],
            "observation_id": supervisor.log["request"]["observations"][-1]["observation_id"],
            "action": "skip_remaining_deployment", "route": "bounded",
            "jev_invocation": {"invoked": False}, "llm_invocation": {"invoked": False}, **overrides}


def write_response(tmp_path, supervisor, **overrides):
    (tmp_path / "response.json").write_text(json.dumps(response(supervisor, **overrides)))


def test_request_contains_only_two_observations_and_bounded_scope(tmp_path):
    supervisor = pending(tmp_path)
    request = json.loads((tmp_path / "request.json").read_text())
    assert set(request) == {"schema", "request_id", "observations", "allowed_actions"}
    assert request["allowed_actions"] == ["hold", "skip_remaining_deployment"]
    assert request["observations"] == supervisor.log["observations"]
    assert request["observations"][1]["time_s"] - request["observations"][0]["time_s"] == 2.
    assert "scenario" not in json.dumps(request) and "recovery" not in json.dumps(request)


def test_instant_broker_reply_waits_for_later_integrated_observation(tmp_path, monkeypatch):
    from pathlib import Path
    original = Path.replace
    def replace_and_respond(path, target):
        result = original(path, target)
        if path.name == "request.json.tmp":
            request = json.loads(Path(target).read_text())
            (tmp_path / "response.json").write_text(json.dumps({
                "request_id": request["request_id"], "observation_id": request["observations"][-1]["observation_id"],
                "action": "skip_remaining_deployment", "route": "bounded", "jev_invocation": {}, "llm_invocation": {}}))
        return result
    monkeypatch.setattr(Path, "replace", replace_and_respond)
    supervisor = pending(tmp_path)
    assert supervisor.log["response"] is None and not supervisor.log["commands"]
    _, command = step(supervisor, 104)
    assert command["time_s"] > supervisor.log["request"]["observations"][-1]["time_s"]


def test_command_requires_later_spaced_observations(tmp_path):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    before, command = step(supervisor, 104)
    assert before["sequencer_state"] == "inhibited"
    assert command["time_s"] == before["time_s"] and command["observation_id"] == before["observation_id"]
    assert supervisor.log["status"] == "command_accepted"
    assert step(supervisor, 105, sequencer_state="skipped") == (None, None)
    first, repeated = step(supervisor, 119, sequencer_state="skipped")
    assert first["sequencer_state"] == "skipped" and repeated is None
    assert supervisor.log["status"] == "command_accepted"
    assert step(supervisor, 133, sequencer_state="skipped") == (None, None)
    second, repeated = step(supervisor, 134, sequencer_state="skipped")
    assert second["time_s"] - first["time_s"] == 15.
    assert repeated is None and len(supervisor.log["commands"]) == 1
    assert supervisor.log["status"] == "effect_observations_recorded"
    assert step(supervisor, 150, sequencer_state="skipped") == (None, None)


def test_receipt_does_not_forge_simulator_effect(tmp_path):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    _, command = step(supervisor, 104)
    assert command is not None
    # An executor that fails to apply the command remains visible. The monitor
    # records these facts for an independent verifier rather than fixing them.
    first, _ = step(supervisor, 119)
    second, _ = step(supervisor, 134)
    assert first["sequencer_state"] == second["sequencer_state"] == "inhibited"
    assert supervisor.log["commands"][0]["sequencer_state_after"] == "skipped"
    assert supervisor.log["status"] == "effect_observations_recorded"
    assert "verified" not in supervisor.log


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"request_id": "another-exchange"}, "request_id_mismatch"),
    ({"observation_id": "an-old-observation"}, "observation_id_mismatch"),
    ({"action": "resume"}, "action_not_allowed"),
    ({"route": "human_review"}, "route_requires_hold"),
    ({"route": "need_observation"}, "route_requires_hold"),
    ({"route": "new-route"}, "route_invalid"),
])
def test_bound_proposal_rejection(tmp_path, overrides, reason):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor, **overrides)
    assert step(supervisor, 104)[1] is None
    assert reason in supervisor.log["rules"][-1]["reasons"]
    assert not supervisor.log["commands"]


@pytest.mark.parametrize("overrides", [
    {"propellant_kg": 27999.}, {"release_attempt_count": 2},
    {"release_acknowledged": False}, {"payload_released_count": 1}, {"sequencer_state": "skipped"},
])
def test_revalidate_current_state_not_proposal_snapshot(tmp_path, overrides):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    assert step(supervisor, 104, **overrides)[1] is None
    assert supervisor.log["status"] == "rejected"
    assert not supervisor.log["commands"]


def test_unbound_current_orbit_rejects_dispatch(tmp_path):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    _, command = supervisor.step(telemetry(104), return_reserve_kg=28000., payload_interval_s=15., orbit_bound=False)
    assert command is None
    assert supervisor.log["rules"][-1]["reasons"] == ["orbit_not_bound"]


@pytest.mark.parametrize("route", ["bounded", "need_observation", "human_review", "deep_reasoning"])
def test_hold_never_dispatches(tmp_path, route):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor, action="hold", route=route)
    assert step(supervisor, 104)[1] is None
    assert supervisor.log["status"] == "held" and not supervisor.log["commands"]


@pytest.mark.parametrize("t", [178, 500])
def test_late_response_is_never_applied(tmp_path, t):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    assert step(supervisor, t)[1] is None
    assert supervisor.log["status"] == "expired"
    assert supervisor.log["response"] is None and not supervisor.log["commands"]


def test_fresh_response_at_return_deadline_is_denied(tmp_path):
    supervisor = pending(tmp_path)
    write_response(tmp_path, supervisor)
    assert step(supervisor, 104, return_deadline_s=104)[1] is None
    assert supervisor.log["rules"][-1]["reasons"] == ["return_deadline"]


@pytest.mark.parametrize("raw", [
    b"not-json", b"[]", b'{"request_id":"x","request_id":"y"}', b'"' + b"x" * 40000 + b'"',
])
def test_malformed_response_fails_closed(tmp_path, raw):
    supervisor = pending(tmp_path)
    (tmp_path / "response.json").write_bytes(raw)
    assert step(supervisor, 104)[1] is None
    assert supervisor.log["rules"][-1]["reasons"] == ["malformed_response"]


def test_nonfinite_nested_invocation_cannot_escape_into_artifacts(tmp_path):
    supervisor = pending(tmp_path)
    raw = json.dumps(response(supervisor, llm_invocation={"elapsed": 1.})).replace('"elapsed": 1.0', '"elapsed": 1e999')
    (tmp_path / "response.json").write_text(raw)
    assert step(supervisor, 104)[1] is None
    assert supervisor.log["rules"][-1]["reasons"] == ["malformed_response"]


def test_mailbox_reuse_is_rejected(tmp_path):
    pending(tmp_path)
    with pytest.raises(FileExistsError):
        FlightSupervision(tmp_path, "another-run")


def test_unfinished_followup_mailbox_cannot_be_reused(tmp_path):
    (tmp_path / "request-followup.json.tmp").write_text("unfinished")
    with pytest.raises(FileExistsError):
        FlightSupervision(tmp_path, "another-run", collect_observation=True)


def test_no_broker_does_not_pause_or_command():
    supervisor = FlightSupervision(None, "fixture-hold")
    step(supervisor, 100)
    step(supervisor, 102)
    assert supervisor.log["status"] == "no_broker_hold"
    assert not supervisor.log["commands"]
    supervisor.pace(10000)


def test_pending_clock_keeps_full_wall_time_for_large_step(tmp_path, monkeypatch):
    clock = [10.]
    sleeps = []
    monkeypatch.setattr("src.runtime.starship_flight_supervision.time.monotonic", lambda: clock[0])
    def sleep(duration):
        sleeps.append(duration)
        clock[0] += duration
    monkeypatch.setattr("src.runtime.starship_flight_supervision.time.sleep", sleep)
    supervisor = pending(tmp_path)
    supervisor.pace(105.5)
    assert sum(sleeps) == 3.5 and max(sleeps) <= 1.
    assert supervisor.log["status"] == "pending"


def test_source_no_effect_fault_continues_real_sixdof(tmp_path):
    """Run the production integration through the first inhibited attempt."""
    from pathlib import Path
    from src.runtime.starship_sixdof_mission import simulate
    profile = json.loads((Path(__file__).resolve().parents[2] / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    run = simulate(profile, scenario="deployment_no_effect", duration_s=532.)
    log = run["supervision"]
    assert log["status"] == "no_broker_hold" and log["final_sequencer_state"] == "inhibited"
    assert run["outcome"]["payload_released_count"] == 0 and not run["satellites"]
    assert sum(e["event"] == "payload_release_attempt_acknowledged" for e in run["events"]) == 1
    assert not any(e["event"] == "payload_released" for e in run["events"])
    assert log["observations"][1]["time_s"] > log["observations"][0]["time_s"]
    observed = [s for s in run["samples"] if "observation_id" in s.get("flight_supervision", {})]
    assert len(observed) == 2
    assert observed[0]["r_eci_m"] != observed[1]["r_eci_m"]
    assert observed[0]["q_body_to_eci"] != observed[1]["q_body_to_eci"]
    assert run["samples"][-1]["flight_supervision"]["sequencer_state"] == "inhibited"


def collection_pending(tmp_path):
    supervisor = FlightSupervision(tmp_path, "test-exchange", collect_observation=True)
    step(supervisor, 100)
    step(supervisor, 102)
    write_response(tmp_path, supervisor, action="hold", route="need_observation")
    assert step(supervisor, 104)[1] is None
    assert supervisor.log["status"] == "collecting"
    assert not supervisor.log["commands"] and not supervisor.log["followup_request"]
    return supervisor


def test_collection_ack_needs_a_later_report_then_later_rules_state(tmp_path):
    supervisor = collection_pending(tmp_path)
    assert step(supervisor, 105) == (None, None)
    report, command = step(supervisor, 106)
    assert command is None and report["time_s"] > supervisor.log["collection"]["issued_time_s"]
    request = supervisor.log["followup_request"]
    assert [o["time_s"] for o in request["observations"]] == [104., 106.]
    following = response(supervisor, observation_id=report["observation_id"])
    (tmp_path / "response-followup.json").write_text(json.dumps(following))
    assert step(supervisor, 107) == (None, None)
    _, command = step(supervisor, 108)
    assert command["time_s"] == 108 and len(supervisor.log["commands"]) == 1
    assert supervisor.log["response"]["route"] == "need_observation"
    assert supervisor.log["followup_response"]["route"] == "bounded"


def test_collection_does_not_reset_the_first_decision_deadline(tmp_path):
    supervisor = collection_pending(tmp_path)
    step(supervisor, 176)
    latest = supervisor.log["followup_request"]["observations"][-1]
    (tmp_path / "response-followup.json").write_text(json.dumps(response(supervisor, observation_id=latest["observation_id"])))
    assert step(supervisor, 178)[1] is None
    assert supervisor.log["status"] == "expired" and not supervisor.log["commands"]


def test_repeated_collection_route_cannot_request_another_report(tmp_path):
    supervisor = collection_pending(tmp_path)
    observation, _ = step(supervisor, 106)
    (tmp_path / "response-followup.json").write_text(json.dumps(response(supervisor,
        observation_id=observation["observation_id"], route="need_observation", action="hold")))
    assert step(supervisor, 108)[1] is None
    assert supervisor.log["status"] == "held" and not supervisor.log["commands"]
    assert not (tmp_path / "request-followup-2.json").exists()


def test_collection_request_fails_without_current_fuel_reserve(tmp_path):
    supervisor = FlightSupervision(tmp_path, "test-exchange", collect_observation=True)
    step(supervisor, 100)
    step(supervisor, 102)
    write_response(tmp_path, supervisor, action="hold", route="need_observation")
    assert step(supervisor, 104, propellant_kg=100.)[1] is None
    assert supervisor.log["collection"] is None and supervisor.log["status"] == "rejected"

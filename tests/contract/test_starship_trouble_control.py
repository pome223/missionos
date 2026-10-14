"""Fault-control authority, provider boundary and actual post-action evidence."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle, simulate_concurrent
from src.runtime.starship_trouble_engine import TroubleEngine, fixture_manifest, make_manifest
from src.runtime.starship_trouble_control import (
    TroubleControl, authority, check_action, digest, observe, validate_observation,
    valid_live_response,
    record_terminal_interruptions, recent_actions, fault_observed,
)
from src.runtime.starship_trouble_control_verifier import verify_run
from src.intelligence.starship_trouble_agent import TroubleAgent, fixture_decision, public_input

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


def setup(profile, mode="fixture", duration=800.):
    manifest = fixture_manifest([{"family": "payload_jam", "delay_s": 0., "target": "dispenser"}])
    grant = authority(profile, manifest, {}, duration, mode,
        provider_models={"jev": "jev-1.13.0", "deepseek": "deepseek-test"} if mode == "live" else None)
    v = vehicle(profile)
    s = dyn.State6DOF(500., (7_000_000., 0., 0.), (0., 7500., 0.), dyn.IDENTITY, dyn.ZERO,
        100_000., tuple(dyn.EngineState() for _ in v.engines), tuple(0. for _ in v.aero_panels))
    engine = TroubleEngine(manifest)
    row = observe(s, v, engine, "ship", "orbital_coast", sequencer="running", return_deadline=2280.)
    actor = TroubleControl(grant, profile, manifest, {}, fixture=lambda req: fixture_decision(req, grant)) if mode == "fixture" else None
    if actor is not None:
        actor.step(row, asdict(s))
        request = actor.pending["ship"]["request"]
    else:
        fixture = TroubleControl(authority(profile, manifest, {}, duration, "fixture"), profile, manifest, {})
        fixture.step(row, asdict(s))
        request = fixture.pending["ship"]["request"]
        request["authority_sha256"] = digest(grant)
    return manifest, grant, s, v, engine, row, actor, request


def test_provider_input_excludes_latent_manifest_and_profile(profile):
    _, grant, _, _, _, row, _, request = setup(profile)
    public = public_input(request, grant)
    encoded = json.dumps(public)
    for forbidden in ("manifest", "source_sha256", "seed", "delay_s", "inertia", "propellant_capacity", "allowed_families"):
        assert forbidden not in encoded
    assert public["observation"] == row
    assert public["observation"]["component_health"]["payload_mechanism"] == "clear"
    assert grant["maximum_llm_calls"] is None


@pytest.mark.parametrize("key", ("seed", "next_fault", "family", "manifest"))
def test_unknown_observation_fields_rejected(profile, key):
    *_, row, actor, request = setup(profile)
    row[key] = "future"
    with pytest.raises(ValueError, match="fields"):
        validate_observation(row)
    request["observation"] = row
    with pytest.raises(ValueError):
        public_input(request, actor.grant)


def test_provider_component_identifier_cannot_carry_manifest_text(profile):
    *_, row, _, _ = setup(profile)
    row["component_health"]["engine_health"][0]["component"] = "seed=12,next_fault=payload_jam"
    with pytest.raises(ValueError, match="health_entry"):
        validate_observation(row)


def test_health_change_rejects_stale_continue_and_fallback_is_independently_bounded(profile):
    _, grant, s, v, engine, _, actor, request = setup(profile)
    s, v = engine.before_step(replace(s, time_s=500.1), v, "ship", "orbital_coast")
    row = observe(s, v, engine, "ship", "orbital_coast", sequencer="running", return_deadline=2280.)
    assert check_action(grant, "continue_mission", row, request) == "continue_observation_stale"
    assert actor.step(row, asdict(s)) == "withhold_deployment"
    assert actor.records[0]["fallback"] is True
    assert actor.records[0]["later_observation"] is None
    row = observe(replace(s, time_s=500.2), v, engine, "ship", "orbital_coast", sequencer="skipped", withheld=True, return_deadline=2280.)
    actor.step(row, asdict(replace(s, time_s=500.2)))
    assert actor.records[0]["later_observation"]["deployment_withheld"] is True


def test_grant_cannot_expand_to_qualified_return_or_actuation(profile):
    _, grant, _, _, _, row, _, request = setup(profile)
    for action in ("state_return", "capture", "ignite", "resume", "grant_scope", "change_attitude"):
        assert check_action(grant, action, row, request) == "outside_approved_actions"
    row["body"] = "booster"
    assert check_action(grant, "withhold_deployment", row) == "deployment_authority_not_applicable"
    assert check_action(grant, "inhibit_return", row) == "return_already_committed_or_unavailable"


class MockTransport:
    """Decoded transport fixture; no provider invocation evidence."""
    def __init__(self, choice, llm=None):
        self.choice, self.llm, self.payloads = choice, llm, []

    def _invoke(self, provider, payload, receipt, decode):
        self.payloads.append((provider, payload))
        if provider == "typesafe":
            keys = payload["questions"]["decision"]["criteria"]
            data = {"model": payload["model"], "answers": {"decision": {"type": "choice", "choice": self.choice,
                "probabilities": {k: float(k == self.choice) for k in keys}, "confidence": 1.}}}
        else:
            data = {"model": payload["model"], "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(self.llm), "reasoning_content": "PRIVATE_NOT_TO_BE_SAVED"}}]}
        result, _ = decode(data)
        return result


def test_jev_can_route_to_deepseek_only_with_orbital_headroom(profile, monkeypatch):
    _, grant, _, _, _, _, _, request = setup(profile, "live")
    monkeypatch.setattr("src.intelligence.starship_trouble_agent.require_live_configuration", lambda grant: None)
    monkeypatch.setattr("src.intelligence.starship_trouble_agent._deepseek_model", lambda: "deepseek-test")
    transport = MockTransport("deep_reasoning", {"action": "collect_status", "summary": "Collect a fresh diagnostic before changing deployment.", "evidence_ids": ["payload_mechanism", "fuel_kg"]})
    response = TroubleAgent(grant, transport).assess(request)
    assert response["action"] == "collect_status"
    assert response["evidence_values"] == {"payload_mechanism": "clear", "fuel_kg": 100000.}
    assert "PRIVATE_NOT_TO_BE_SAVED" not in json.dumps(response)
    assert transport.payloads[1][1]["max_tokens"] == 16384
    serialized_contract = transport.payloads[1][1]["messages"][0]["content"]
    assert "exactly action, summary, evidence_ids; no extra keys" in serialized_contract
    assert "1 to 600 characters" in serialized_contract
    assert "1 to 12 unique string IDs" in serialized_contract
    assert "8000 characters" in serialized_contract
    assert response["model_inference_invoked"] is False
    short = deepcopy(request)
    short["observation"]["phase"] = "ship_ascent"
    short["observation"]["return_deadline_s"] = None
    short["deadline_s"] = 512.
    short["deep_reasoning_allowed"] = False
    short["allowed_actions"].remove("inhibit_return")
    with pytest.raises(ValueError, match="choice"):
        TroubleAgent(grant, MockTransport("deep_reasoning")).assess(short)


@pytest.fixture(scope="module")
def physical_record():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    manifest = fixture_manifest([{"family": "ascent_engine_out", "delay_s": 20., "target": "booster_main_0"}])
    grant = authority(profile, manifest, {}, 25., "fixture")
    actor = TroubleControl(grant, profile, manifest, {}, fixture=lambda req: fixture_decision(req, grant))
    run = simulate_concurrent(profile, duration_s=25., trouble_manifest=manifest, trouble_control=actor)
    return profile, grant, json.loads(json.dumps(run, allow_nan=False))


def test_actual_fault_control_observed_after_command(physical_record):
    profile, grant, run = physical_record
    verified = verify_run(run, profile, grant)
    assert verified["passed"], verified
    records = run["trouble_control"]["records"]
    stop = next(row for row in records if row["action"] == "withhold_deployment")
    assert stop["later_observation"]["time_s"] > stop["time_s"]
    assert stop["later_observation"]["deployment_withheld"] is True
    assert run["trouble"]["receipts"][0]["family"] == "ascent_engine_out"
    assert any(row["action"] == "collect_status" for row in records)
    assert verified["accepted_live_decisions"] == 0


@pytest.mark.parametrize("tamper", ("later", "witness", "command", "leak", "authority", "diagnostic", "fallback", "history"))
def test_independent_verifier_rejects_forged_evidence(physical_record, tamper):
    profile, grant, source = physical_record
    run = deepcopy(source)
    row = next(row for row in run["trouble_control"]["records"] if row["action"] == "withhold_deployment")
    if tamper == "later":
        row["later_observation"]["time_s"] = row["time_s"]
    elif tamper == "witness":
        run["trouble_control"]["observation_witnesses"][0]["state"]["propellant_kg"] += 1.
    elif tamper == "command":
        run["events"] = [event for event in run["events"] if event["event"] != "trouble_control_command"]
    elif tamper == "leak":
        row["request"]["observation"]["seed"] = 1
    elif tamper == "diagnostic":
        collect = next(item for item in run["trouble_control"]["records"] if item["action"] == "collect_status")
        collect["diagnostic_observation"] = deepcopy(collect["later_observation"])
    elif tamper == "fallback":
        collect = next(item for item in run["trouble_control"]["records"] if item["action"] == "collect_status")
        collect.update(fallback=True, accepted_action=None, rejection="invalid_or_missing_response")
    elif tamper == "history":
        row["request"]["recent_actions"] = [{"time_s": 0., "action": "continue_mission",
            "diagnostic_status": "not_requested", "diagnostic_health_unchanged": None}]
    else:
        run["trouble_control"]["authority"]["fault_return_qualified"] = True
    assert not verify_run(run, profile, grant)["passed"]


def test_trouble_cannot_reuse_m3_or_nominal_return_authority(profile):
    manifest = make_manifest(1, nominal_probability=1.)
    grant = authority(profile, manifest, {}, 1., "fixture")
    actor = TroubleControl(grant, profile, manifest, {})
    with pytest.raises(ValueError, match="standalone"):
        simulate_concurrent(profile, duration_s=1., trouble_manifest=manifest, trouble_control=actor, mission_case="normal")
    with pytest.raises(ValueError, match="binding"):
        simulate_concurrent(profile, duration_s=2., trouble_manifest=manifest, trouble_control=actor)


def test_absent_control_preserves_default_and_monitor_only_preserves_state(profile):
    plain = simulate_concurrent(profile, duration_s=1.)
    assert simulate_concurrent(profile, duration_s=1., trouble_control=None) == plain
    manifest = make_manifest(7, nominal_probability=1.)
    grant = authority(profile, manifest, {}, 1., "fixture")
    actor = TroubleControl(grant, profile, manifest, {}, fixture=lambda req: fixture_decision(req, grant))
    monitored = simulate_concurrent(profile, duration_s=1., trouble_manifest=manifest, trouble_control=actor)
    assert monitored["final_state"] == plain["final_state"]
    assert monitored["outcome"] == plain["outcome"]


def test_cli_approval_spawn_and_no_reuse(tmp_path, monkeypatch):
    script = ROOT/"scripts/run_starship_trouble_control.py"
    output = tmp_path/"cli"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "DEEPSEEK_API_KEY": "fixture-key-must-not-reach-child"}
    def command(*args):
        return subprocess.run([sys.executable, str(script), *args, "--output-dir", str(output)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
    prepared = command("--prepare", "--seed", "1", "--nominal-probability", "1", "--duration-s", "1")
    assert prepared.returncode == 0, prepared.stderr
    token = json.loads(prepared.stdout)["plan_sha256"]
    wrong = command("--approve-plan-sha256", "0"*64)
    assert wrong.returncode != 0
    assert not (output/"execution.claimed").exists()
    executed = command("--approve-plan-sha256", token)
    assert executed.returncode == 0, executed.stdout+executed.stderr+(output/"worker.log").read_text()
    verification = json.loads((output/"verification.json").read_text())
    assert verification["passed"], verification
    assert json.loads((output/"host-execution.json").read_text())["provider_attempts"] == 0
    assert command("--approve-plan-sha256", token).returncode != 0
    original = (output/"study.json").read_bytes()
    credential_names = ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    clean_env = {key: value for key, value in env.items() if key not in credential_names}
    replay = subprocess.run([sys.executable, str(script), "--worker", "--output-dir", str(output)],
        cwd=ROOT, env=clean_env, text=True, capture_output=True, timeout=20)
    assert replay.returncode != 0
    assert "worker.started" in replay.stderr and "FileExistsError" in replay.stderr
    assert "provider_credentials_in_worker" not in replay.stderr
    # Verify the one-use worker claim prevents entering physics, independently
    # of the credential guard and of later output-file collision checks.
    import runpy
    from src.runtime import starship_sixdof_mission as mission
    entered = []
    monkeypatch.setattr(mission, "simulate_concurrent", lambda *args, **kwargs: entered.append(True))
    for name in credential_names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(FileExistsError, match="worker.started"):
        runpy.run_path(str(script))["worker"](output)
    assert entered == []
    assert (output/"study.json").read_bytes() == original
    assert "fixture-key-must-not-reach-child" not in (output/"worker.log").read_text()


def confirmed_response(request, grant):
    """Synthetic receipt fixture used ONLY to test record validators."""
    from src.intelligence.starship_flight_supervisor import _receipt
    public = public_input(request, grant)
    receipt = _receipt("live", "typesafe", public)
    receipt.update(status="succeeded", requested_model_id=grant["provider_models"]["jev"],
        model_id=grant["provider_models"]["jev"], call_attempted=True, response_received=True,
        complete_response_observed=True, call_succeeded=True, model_inference_invoked=True,
        model_inference_status="confirmed_by_valid_response", request_sha256="a"*64,
        response_sha256="b"*64, request_bytes=1000, response_bytes_read=500,
        latency_ms=10., timeout_seconds=30., maximum_instance_calls=None,
        selected_choice="continue_mission")
    llm = _receipt("live", "deepseek", public)
    llm["status"] = "not_routed"
    return {"request_id": request["request_id"], "request_sha256": digest(request), "mode": "live",
        "action": "continue_mission", "route": "continue_mission", "summary": "", "evidence_ids": [], "evidence_values": {},
        "model_inference_invoked": True, "selected_invocation": receipt, "jev_invocation": receipt, "llm_invocation": llm}


def test_payload_diagnostic_is_not_applicable_on_booster(profile):
    manifest, _, state, vehicle_, engine, _, _, _ = setup(profile)
    engine.before_step(state, vehicle_, "ship", "orbital_coast")
    ship = observe(state, vehicle_, engine, "ship", "orbital_coast")
    booster = observe(state, vehicle_, engine, "booster", "booster_entry_coast")
    assert ship["component_health"]["payload_mechanism"] == "blocked"
    assert booster["component_health"]["payload_mechanism"] == "blocked"
    assert engine.observation(state, vehicle_, "booster")["payload_mechanism"] == "blocked"
    assert not fault_observed(booster) and fault_observed(ship)
    grant = authority(profile, manifest, {}, 800., "fixture")
    actor = TroubleControl(grant, profile, manifest, {})
    actor.step(booster, asdict(state))
    request = actor.pending["booster"]["request"]
    public = public_input(request, grant)
    assert public["observation"]["component_health"]["payload_mechanism"] == "not_applicable"
    assert public["evidence"]["payload_mechanism"] == "not_applicable"
    assert request["observation"]["component_health"]["payload_mechanism"] == "blocked"


@pytest.mark.parametrize("phase", ("landing_burn", "booster_landing_burn"))
def test_diagnostic_requests_not_admitted_during_terminal_burn(profile, phase):
    _, grant, _, _, _, row, _, _ = setup(profile)
    row["phase"] = phase
    assert check_action(grant, "collect_status", row) == "diagnostic_window_unavailable"
    assert check_action(grant, "continue_mission", row) is None


def test_diagnostic_needs_remaining_horizon_and_return_window(profile):
    _, grant, _, _, _, row, _, _ = setup(profile)
    row["time_s"] = 799.
    row["component_health"]["observed_at_s"] = 799.
    assert check_action(grant, "collect_status", row) == "diagnostic_window_unavailable"
    row["time_s"] = row["component_health"]["observed_at_s"] = 500.
    row["return_deadline_s"] = 501.
    assert check_action(grant, "collect_status", row) == "diagnostic_window_unavailable"


def test_terminal_interruption_is_explicit_never_a_completed_observation(profile):
    _, _, _, _, _, row, _, _ = setup(profile)
    item = {"time_s": 500., "observation": row, "action": "collect_status", "diagnostic_observation": None,
        "diagnostic_completion": {"status": "pending", "time_s": None, "reason": None}}
    run = {"trouble_control": {"records": [item]}, "outcome": {"termination": "surface_contact"},
        "final_state": {"time_s": 501.}}
    record_terminal_interruptions(run)
    assert item["diagnostic_observation"] is None
    assert item["diagnostic_completion"] == {"status": "interrupted", "time_s": 501.,
        "reason": "body_surface_contact", "outcome_sha256": digest(run["outcome"])}


def test_recent_history_only_uses_completion_known_at_request_time(profile):
    _, grant, _, _, _, row, _, _ = setup(profile)
    after = deepcopy(row)
    after["time_s"] = after["component_health"]["observed_at_s"] = 502.
    records = [{"time_s": 500., "action": "collect_status", "observation": row, "diagnostic_observation": after}]
    pending = recent_actions(records, "ship", 501., grant["recent_action_limit"])
    assert pending[0]["diagnostic_status"] == "pending" and pending[0]["diagnostic_health_unchanged"] is None
    completed = recent_actions(records, "ship", 502., grant["recent_action_limit"])
    assert completed[0]["diagnostic_status"] == "completed" and completed[0]["diagnostic_health_unchanged"] is True


def test_missing_later_sample_cannot_hide_incomplete_diagnostic(physical_record):
    profile, grant, original = physical_record
    run = deepcopy(original)
    row = next(x for x in run["trouble_control"]["records"] if x["action"] == "collect_status")
    row["later_observation"] = None
    row["diagnostic_observation"] = None
    row["diagnostic_completion"] = {"status": "interrupted", "time_s": row["time_s"]+.1,
        "reason": "body_surface_contact", "outcome_sha256": "0"*64}
    result = verify_run(run, profile, grant)
    assert not result["passed"] and not result["case_accepted"]
    assert result["all_requested_diagnostics_completed"] is False


def test_fake_contact_cannot_explain_missing_diagnostic(physical_record):
    profile, grant, original = physical_record
    run = deepcopy(original)
    row = next(x for x in run["trouble_control"]["records"] if x["action"] == "collect_status")
    row["diagnostic_observation"] = None
    row["diagnostic_completion"] = {"status": "interrupted", "time_s": row["time_s"]+1.,
        "reason": "body_surface_contact", "outcome_sha256": digest(run["outcome"])}
    result = verify_run(run, profile, grant)
    assert not result["passed"] and not result["case_accepted"]
    assert any(code.startswith("unproved_diagnostic_interruption") for code in result["errors"])


def test_booster_control_witness_preserves_raw_physical_diagnostics(profile):
    manifest = make_manifest(1, nominal_probability=1.)
    grant = authority(profile, manifest, {}, 140., "fixture")
    actor = TroubleControl(grant, profile, manifest, {}, fixture=lambda req: fixture_decision(req, grant))
    run = json.loads(json.dumps(simulate_concurrent(profile, duration_s=140.,
        trouble_manifest=manifest, trouble_control=actor), allow_nan=False))
    result = verify_run(run, profile, grant)
    assert result["passed"] and result["case_accepted"], result
    rows = [row for row in run["trouble_control"]["records"] if row["observation"]["body"] == "booster"]
    assert rows
    for row in rows:
        assert row["observation"]["component_health"]["payload_mechanism"] == "clear"
        public = public_input(row["request"], grant)
        assert public["observation"]["component_health"]["payload_mechanism"] == "not_applicable"


@pytest.mark.parametrize("field,bad", [("schema_version", "wrong"), ("provider", "wrong"), ("model_id", "wrong"),
    ("call_attempted", False), ("response_received", False), ("complete_response_observed", False),
    ("call_succeeded", False), ("request_sha256", None), ("response_sha256", "bad"),
    ("public_input_sha256", "0"*64), ("selected_choice", "deep_reasoning"), ("timeout_seconds", 151.),
    ("latency_ms", 151000.), ("maximum_instance_calls", 1)])
def test_contradictory_live_receipts_rejected(profile, field, bad):
    _, grant, _, _, _, _, _, request = setup(profile, "live")
    response = confirmed_response(request, grant)
    assert valid_live_response(response, request, grant)
    response["selected_invocation"][field] = bad
    assert not valid_live_response(response, request, grant)


def test_provider_model_change_stops_before_invocation(profile, monkeypatch):
    _, grant, _, _, _, _, _, request = setup(profile, "live")
    monkeypatch.setattr("src.intelligence.starship_trouble_agent.require_live_configuration", lambda grant: None)
    monkeypatch.setattr("src.intelligence.starship_trouble_agent._deepseek_model", lambda: "deepseek-other")
    transport = MockTransport("continue_mission")
    with pytest.raises(ValueError, match="provider_model_changed"):
        TroubleAgent(grant, transport).assess(request)
    assert transport.payloads == []

"""Tamper checks for independently saved fault evidence; no flight/model runs."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_trouble_engine import TroubleEngine, fixture_manifest, make_manifest
from src.runtime.starship_trouble_verifier import verify_run


def _saved_engine_fault():
    v = dyn.Vehicle6DOF(100., dyn.ZERO, ((100., 0., 0.), (0., 110., 0.), (0., 0., 120.)),
                       20., dyn.ZERO, 1., 2., (dyn.Engine("ship_main_0", dyn.ZERO, 100., 300.),))
    s = dyn.State6DOF(0., (7_000_000., 0., 0.), dyn.ZERO, dyn.IDENTITY, dyn.ZERO, 20., (dyn.EngineState(throttle=1.),))
    fault = TroubleEngine(fixture_manifest([dict(family="ship_engine_out", delay_s=5., target="ship_main_0")]))
    fault.before_step(s, v, "ship", "ship_ascent")
    initial = _sample(s, v, fault, "ship_ascent")
    failed, _ = fault.before_step(replace(s, time_s=5.), v, "ship", "ship_ascent")
    command = dyn.Command6DOF((dyn.EngineCommand(True, 1.),))
    final = dyn.step(failed, v, command, .1, gravity=False, atmosphere=False)
    last = _sample(final, v, fault, "ship_ascent", command)
    return json.loads(json.dumps(dict(trouble=fault.audit(), samples=[initial, last],
        initial_state=initial, final_state=asdict(final), final_vehicle=asdict(v),
        booster_run=None, booster_separation_state=None)))


def _sample(s, v, fault, phase, command=None, body="ship"):
    return dict(**asdict(s), phase=phase, aero_panel_names=[p.name for p in v.aero_panels],
                controller=dict(component_health=fault.observation(s, v, body)),
                command=asdict(command) if command else None)


def _rehash(run):
    chain = run["trouble"]["manifest_sha256"]
    for receipt in run["trouble"]["receipts"]:
        receipt["previous_sha256"] = chain
        payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        chain = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        receipt["receipt_sha256"] = chain


def test_fault_records_match_frozen_manifest_and_observed_unavailable_engine():
    result = verify_run(_saved_engine_fault())
    assert result["passed"], result
    assert result["applied_event_count"] == 1
    assert result["unapplied_event_count"] == 0
    assert result["sampled_effect_checks"] == 1
    assert result["claim"] == "record_consistency_only_not_recovery_or_ai_success"


def test_verifier_rejects_hash_tamper_and_late_application_even_after_rehash():
    run = _saved_engine_fault()
    run["trouble"]["receipts"][0]["effects"][0]["throttle_at_injection"] = .4
    assert "hash_mismatch" in verify_run(run)["errors"][0]
    run = _saved_engine_fault()
    run["trouble"]["receipts"][0]["applied_at_s"] += .01
    _rehash(run)
    assert "trigger_boundary" in verify_run(run)["errors"][0]


@pytest.mark.parametrize("where", ["sample", "final"])
def test_reenabled_engine_is_rejected(where):
    run = _saved_engine_fault()
    if where == "sample":
        run["samples"][-1]["engine_states"][0]["available"] = True
        # Changing the reporting channel too cannot hide changed plant state.
        run["samples"][-1]["controller"]["component_health"]["engine_health"][0]["available"] = True
    else:
        run["final_state"]["engine_states"][0]["available"] = True
    result = verify_run(run)
    assert not result["passed"]
    assert "reenabled" in result["errors"][0]


def test_health_cannot_disagree_with_saved_states_or_leak_private_fields():
    run = _saved_engine_fault()
    run["samples"][-1]["controller"]["component_health"]["engine_health"][0]["available"] = True
    assert "health_disagrees" in verify_run(run)["errors"][0]
    run = _saved_engine_fault()
    run["samples"][-1]["controller"]["component_health"]["seed"] = 42
    assert "observation_fields" in verify_run(run)["errors"][0]


def test_manifest_or_event_omission_cannot_be_hidden_in_partition():
    run = _saved_engine_fault()
    run["trouble"]["manifest"]["seed"] += 1
    assert not verify_run(run)["passed"]
    run = _saved_engine_fault()
    run["trouble"]["receipts"].clear()
    assert "partition" in verify_run(run)["errors"][0]
    run["trouble"]["unapplied_event_ids"] = ["fault_01"]
    assert "due_fault_missing" in verify_run(run)["errors"][0]


def test_nominal_manifest_does_not_need_an_applied_fault():
    run = _saved_engine_fault()
    run["trouble"] = TroubleEngine(make_manifest(3, nominal_probability=1.)).audit()
    assert verify_run(run)["passed"]  # No claim that all plant faults originate in this engine.


def test_jam_is_checked_against_saved_panel_and_command():
    run = _saved_engine_fault()
    v = dyn.vehicle_from_dict(run["final_vehicle"])
    v = replace(v, aero_panels=(dyn.AeroPanel("flap_0", (0., 0., 1.), (0., 1., 0.), 1.),))
    s = replace(dyn.state_from_dict(run["final_state"]), time_s=0., flap_angles_rad=(.1,))
    fault = TroubleEngine(fixture_manifest([dict(family="flap_jam", delay_s=0., target="movable_panel_0")]))
    s, _ = fault.before_step(s, v, "ship", "ballistic_return")
    initial = _sample(s, v, fault, "ballistic_return")
    command = fault.command(dyn.Command6DOF((dyn.EngineCommand(),), (.3,)), s, v, "ship")
    final = dyn.step(s, v, command, .1, gravity=False, atmosphere=False)
    last = _sample(final, v, fault, "ballistic_return", command)
    run = json.loads(json.dumps(dict(trouble=fault.audit(), samples=[initial, last], initial_state=initial,
        final_state=asdict(final), final_vehicle=asdict(v), booster_run=None, booster_separation_state=None)))
    assert verify_run(run)["passed"]
    changed = deepcopy(run)
    changed["samples"][-1]["command"]["flap_angles_rad"][0] = .3
    assert "command_not_held" in verify_run(changed)["errors"][0]
    changed = deepcopy(run)
    changed["samples"][-1]["flap_angles_rad"][0] += .01
    assert "panel_moved" in verify_run(changed)["errors"][0]
    for value in ([], [dict(component="flap_0", jammed=False)],
                  [dict(component="flap_0", jammed=True), dict(component="invented", jammed=True)]):
        changed = deepcopy(run)
        changed["samples"][-1]["controller"]["component_health"]["actuator_health"] = value
        assert "actuator_health_disagrees" in verify_run(changed)["errors"][0]
    changed = deepcopy(run)
    changed["samples"][-1]["controller"]["component_health"]["actuator_health"][0]["jammed"] = 1
    assert "invalid_actuator_health_flag" in verify_run(changed)["errors"][0]


def test_missing_audit_returns_failed_verdict_not_an_exception():
    assert verify_run({})["passed"] is False


def test_ascent_engine_failure_survives_separation_and_booster_initialization():
    # Saved rigid states are a minimal fixture here; this test checks the
    # verifier's inheritance boundary, not a launch or conservation solution.
    run = _saved_engine_fault()
    v = dyn.vehicle_from_dict(run["final_vehicle"])
    v = replace(v, engines=(replace(v.engines[0], name="booster_main_0"),))
    s = replace(dyn.state_from_dict(run["final_state"]), time_s=0., engine_states=(dyn.EngineState(throttle=1.),))
    trouble = TroubleEngine(fixture_manifest([dict(family="ascent_engine_out", delay_s=20., target="booster_main_0")]))
    trouble.before_step(s, v, "ship", "stack_ascent")
    initial = _sample(s, v, trouble, "stack_ascent")
    failed, _ = trouble.before_step(replace(s, time_s=20.), v, "ship", "stack_ascent")
    separated = replace(failed, time_s=21.)
    booster_initial = dict(**asdict(separated), phase="booster_boostback_slew")
    booster_final = replace(separated, time_s=22.)
    booster = dict(initial_state=booster_initial, final_state=asdict(booster_final), final_vehicle=asdict(v),
                   booster_separation_state=asdict(separated), samples=[booster_initial,
                       dict(**asdict(booster_final), phase="booster_boostback")])
    ship_v = replace(v, engines=(replace(v.engines[0], name="ship_main_0"),))
    ship_final = replace(separated, time_s=30., engine_states=(dyn.EngineState(),))
    last = _sample(ship_final, ship_v, trouble, "ship_ascent")
    run = json.loads(json.dumps(dict(trouble=trouble.audit(), samples=[initial, last], initial_state=initial,
        final_state=asdict(ship_final), final_vehicle=asdict(ship_v), booster_run=booster,
        booster_separation_state=asdict(separated))))
    assert verify_run(run)["passed"], verify_run(run)
    run["booster_run"]["initial_state"]["engine_states"][0]["available"] = True
    assert "availability_not_preserved" in verify_run(run)["errors"][0]


def _saved_payload_jam():
    source = _saved_engine_fault()
    v = dyn.vehicle_from_dict(source["final_vehicle"])
    s = replace(dyn.state_from_dict(source["final_state"]), time_s=0.)
    fault = TroubleEngine(fixture_manifest([dict(family="payload_jam", delay_s=2., target="dispenser")]))
    fault.before_step(s, v, "ship", "orbital_coast")
    initial = _sample(s, v, fault, "orbital_coast")
    fault.before_step(replace(s, time_s=2.), v, "ship", "orbital_coast")
    final = replace(s, time_s=3.)
    last = _sample(final, v, fault, "orbital_coast")
    return json.loads(json.dumps(dict(trouble=fault.audit(), initial_state=initial,
        final_state=asdict(final), final_vehicle=asdict(v), samples=[initial, last],
        booster_run=None, booster_separation_state=None, events=[], satellites=[])))


def test_payload_jam_requires_current_blocked_health():
    run = _saved_payload_jam()
    assert verify_run(run)["passed"], verify_run(run)
    run["samples"][-1]["controller"]["component_health"]["payload_mechanism"] = "clear"
    assert "payload_health_disagrees" in verify_run(run)["errors"][0]


@pytest.mark.parametrize("channel", ("event", "satellite", "receipt"))
@pytest.mark.parametrize("time", (2., 2.1))
def test_physical_payload_release_at_or_after_jam_is_rejected(channel, time):
    run = _saved_payload_jam()
    if channel == "event":
        run["events"] = [dict(event="payload_released", time_s=time)]
    elif channel == "satellite":
        run["satellites"] = [dict(release_state=dict(time_s=time))]
    else:
        run["satellites"] = [dict(release_state=dict(time_s=1.), receipt=dict(time_s=time))]
    assert not verify_run(run)["passed"]
    assert "after_jam" in verify_run(run)["errors"][0]


def test_payload_release_before_later_jam_remains_valid_evidence():
    run = _saved_payload_jam()
    run["events"] = [dict(event="payload_released", time_s=1.)]
    run["satellites"] = [dict(release_state=dict(time_s=1.), receipt=dict(time_s=1.))]
    assert verify_run(run)["passed"], verify_run(run)


def test_tower_health_must_follow_realized_unavailability_for_both_bodies():
    source = _saved_engine_fault()
    v = dyn.vehicle_from_dict(source["final_vehicle"])
    s = replace(dyn.state_from_dict(source["final_state"]), time_s=0.)
    fault = TroubleEngine(fixture_manifest([dict(family="tower_unavailable", delay_s=0., target="tower")]))
    fault.before_step(s, v, "ship", "orbital_coast")
    initial = _sample(s, v, fault, "orbital_coast")
    separated = replace(s, time_s=1.)
    fault.before_step(separated, v, "booster", "booster_boostback_slew")
    booster_initial = _sample(separated, v, fault, "booster_boostback_slew", body="booster")
    final = replace(s, time_s=3.)
    last = _sample(final, v, fault, "orbital_coast")
    booster_last = _sample(final, v, fault, "booster_boostback", body="booster")
    booster = dict(initial_state=booster_initial, final_state=asdict(final), final_vehicle=asdict(v),
        booster_separation_state=asdict(separated), samples=[booster_initial, booster_last])
    run = json.loads(json.dumps(dict(trouble=fault.audit(), initial_state=initial, final_state=asdict(final),
        final_vehicle=asdict(v), samples=[initial, last], booster_run=booster, booster_separation_state=asdict(separated))))
    assert verify_run(run)["passed"], verify_run(run)
    for part in ("ship", "booster"):
        changed = deepcopy(run)
        samples = changed["samples"] if part == "ship" else changed["booster_run"]["samples"]
        samples[-1]["controller"]["component_health"]["tower_ready"] = True
        assert "tower_health_disagrees" in verify_run(changed)["errors"][0]

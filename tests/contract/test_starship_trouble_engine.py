"""Seeded scheduling, no-leak observations and real small-step plant effects."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import math
import random

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_trouble_engine import (
    DEFAULT_FAMILIES, TroubleEngine, fixture_manifest, make_manifest, manifest_digest,
    validate_manifest,
)


def vehicle(engines=(), panels=()):
    return dyn.Vehicle6DOF(100., dyn.ZERO, ((100., 0., 0.), (0., 110., 0.), (0., 0., 120.)),
                          20., dyn.ZERO, 1., 2., engines, panels)


def state(v, time=0.):
    return dyn.State6DOF(time, (7_000_000., 0., 0.), dyn.ZERO, dyn.IDENTITY, dyn.ZERO, 20.,
                        tuple(dyn.EngineState(throttle=1.) for _ in v.engines),
                        tuple(.1 for _ in v.aero_panels))


def engine(family, delay, target):
    return TroubleEngine(fixture_manifest([dict(family=family, delay_s=delay, target=target)]))


def main(name="booster_main_0"):
    return dyn.Engine(name, (1., 0., 0.), 100., 300.)


def test_generation_is_named_seeded_and_does_not_use_global_rng():
    random_state = random.getstate()
    first = make_manifest(731103, max_events=4)
    for seed in range(8):
        make_manifest(seed)
    assert make_manifest(731103, max_events=4) == first
    assert make_manifest(731103, allowed_families=list(reversed(DEFAULT_FAMILIES)), max_events=4) == first
    assert random.getstate() == random_state
    assert first["parameter_basis"] == "synthetic_distribution_not_historical_incident_rates"
    assert first == json.loads(json.dumps(first, allow_nan=False))
    assert 1 <= len(first["events"]) <= 4
    assert len({event["family"] for event in first["events"]}) == len(first["events"])


def test_distribution_controls_nominal_and_allowed_fault_pool():
    assert "flap_jam" not in make_manifest(3)["allowed_families"]
    assert make_manifest(3, allowed_families=["flap_jam"], nominal_probability=0.)["events"][0]["family"] == "flap_jam"
    assert make_manifest(3, nominal_probability=1.)["events"] == []
    assert make_manifest(3, allowed_families=[], max_events=0)["events"] == []
    for seed in range(20):
        value = make_manifest(seed, allowed_families=["payload_jam"], nominal_probability=0.)
        assert len(value["events"]) == 1
        assert value["events"][0]["family"] == "payload_jam"


@pytest.mark.parametrize("seed", (-1, True, 1., "1", 2**63, None))
def test_invalid_seed_rejected(seed):
    with pytest.raises(ValueError, match="seed"):
        make_manifest(seed)


@pytest.mark.parametrize("kwargs", [dict(max_events=True), dict(max_events=-1), dict(max_events=8),
    dict(nominal_probability=math.nan), dict(nominal_probability=True), dict(nominal_probability=1.1),
    dict(allowed_families=["payload_jam", "payload_jam"]), dict(allowed_families=["unregistered"]),
    dict(allowed_families="payload_jam")])
def test_invalid_distribution_rejected(kwargs):
    with pytest.raises(ValueError):
        make_manifest(1, **kwargs)


def test_manifest_digest_freeze_and_strict_generated_schedule():
    value = make_manifest(41, nominal_probability=0.)
    saved = deepcopy(value)
    fault_engine = TroubleEngine(value)
    digest = manifest_digest(value)
    value["events"].clear()
    assert fault_engine.audit()["manifest"] == saved
    assert fault_engine.manifest_sha256 == digest
    changed = fault_engine.audit()
    changed["manifest"]["events"].clear()
    assert fault_engine.audit()["manifest"] == saved
    with pytest.raises(ValueError, match="schedule"):
        validate_manifest(value)
    saved["extra"] = 1
    with pytest.raises(ValueError, match="shape"):
        validate_manifest(saved)


@pytest.mark.parametrize("field,value", [("body", "booster"), ("phase", "landing_burn"),
    ("target", "secret_target"), ("delay_s", -1.), ("delay_s", math.inf),
    ("delay_s", True), ("id", "arbitrary")])
def test_fixture_cannot_bypass_registry_or_bounds(field, value):
    manifest = fixture_manifest([dict(family="payload_jam", delay_s=0., target="dispenser")])
    manifest["events"][0][field] = value
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_event_boundary_is_phase_relative_and_unreached_events_do_nothing():
    v = vehicle((main(),))
    e = engine("ascent_engine_out", 20.25, "booster_main_0")
    s = state(v, 5.)
    s, same_v = e.before_step(s, v, "ship", "stack_ascent")
    assert same_v is v
    assert e.limit_step(5., 40., "ship", "stack_ascent") == 20.25
    almost = replace(s, time_s=math.nextafter(25.25, -math.inf))
    almost, _ = e.before_step(almost, v, "ship", "stack_ascent")
    assert almost.engine_states[0].available
    at = replace(s, time_s=25.25)
    with pytest.raises(ValueError, match="before_step"):
        e.limit_step(at.time_s, 1., "ship", "stack_ascent")
    applied, _ = e.before_step(at, v, "ship", "stack_ascent")
    assert not applied.engine_states[0].available
    e.before_step(applied, v, "ship", "stack_ascent")
    assert len(e.audit()["receipts"]) == 1
    assert e.audit()["receipts"][0]["scheduled_at_s"] == 25.25
    assert e.audit()["receipts"][0]["applied_at_s"] == 25.25
    never = engine("payload_jam", 0., "dispenser")
    never.before_step(state(v), v, "ship", "stack_ascent")
    assert not never.payload_blocked(100000.)
    assert never.audit()["unapplied_event_ids"] == ["fault_01"]


def test_engine_out_removes_actual_force_without_teleport_or_fuel_overwrite():
    v = vehicle((main(),))
    e = engine("ascent_engine_out", 20., "booster_main_0")
    original = state(v)
    e.before_step(original, v, "ship", "stack_ascent")
    before = replace(original, time_s=20.)
    after, _ = e.before_step(before, v, "ship", "stack_ascent")
    assert after == replace(before, engine_states=(replace(before.engine_states[0], available=False),))
    assert after.engine_states[0].throttle == 1.
    command = dyn.Command6DOF((dyn.EngineCommand(True, 1.),))
    healthy = dyn.step(before, v, command, .1, gravity=False, atmosphere=False)
    failed = dyn.step(after, v, command, .1, gravity=False, atmosphere=False)
    assert dyn.observe(failed, v, atmosphere=False)["thrust_force_body_n"] == dyn.ZERO
    assert failed.v_eci_mps == before.v_eci_mps
    assert healthy.v_eci_mps != before.v_eci_mps
    assert failed.propellant_kg == before.propellant_kg
    assert healthy.propellant_kg < before.propellant_kg
    assert failed.engine_states[0].throttle < after.engine_states[0].throttle


def test_rcs_channel_loss_disables_only_the_selected_physical_torque_pair():
    engines = tuple(dyn.Engine(f"rcs_0_1_{side}", (0., side, 0.), 100., 300.,
                               direction_body=(0., 0., side), min_throttle=0.) for side in (-1, 1))
    v = vehicle(engines + (main("ship_main_0"),))
    e, before = engine("rcs_channel_loss", 0., "rcs_0_1"), state(v)
    after, _ = e.before_step(before, v, "ship", "orbital_coast")
    assert [x.available for x in after.engine_states] == [False, False, True]
    loads = dyn.observe(after, v, atmosphere=False)["engine_loads"]
    assert [x["thrust_n"] for x in loads] == [0., 0., 100.]
    assert [x["component"] for x in e.audit()["receipts"][0]["effects"]] == ["rcs_0_1_-1", "rcs_0_1_1"]


def test_flap_jam_freezes_measured_angle_through_actuator_command_not_state():
    panels = tuple(dyn.AeroPanel(f"flap_{i}", (0., 0., i+1.), (0., 1., 0.), 1.) for i in range(2))
    v = vehicle(panels=panels)
    before = state(v)
    e = engine("flap_jam", 0., "movable_panel_0")
    after, same_v = e.before_step(before, v, "ship", "ballistic_return")
    assert after is before and same_v is v
    requested = dyn.Command6DOF(flap_angles_rad=(.3, -.3))
    actual = e.command(requested, after, v, "ship")
    assert actual.flap_angles_rad == (.1, -.3)
    assert requested.flap_angles_rad == (.3, -.3)
    integrated = dyn.step(after, v, actual, .1, gravity=False, atmosphere=False)
    assert integrated.flap_angles_rad[0] == .1
    assert -.3 < integrated.flap_angles_rad[1] < .1
    assert e.audit()["receipts"][0]["effects"][0]["held_angle_rad"] == .1


def test_payload_and_tower_signals_change_only_on_applied_tick():
    manifest = fixture_manifest([
        dict(family="payload_jam", delay_s=2., target="dispenser"),
        dict(family="tower_unavailable", delay_s=0., target="tower"),
    ])
    e, v = TroubleEngine(manifest), vehicle()
    assert not e.payload_blocked(1000.) and e.tower_ready(1000.)
    e.before_step(state(v, 100.), v, "ship", "orbital_coast")
    e.before_step(state(v, 50.), v, "booster", "booster_boostback_slew")
    assert e.tower_ready(49.) and not e.tower_ready(50.)
    assert not e.payload_blocked(101.)
    e.before_step(state(v, 102.), v, "ship", "orbital_coast")
    assert e.payload_blocked(102.)
    # Another body can run earlier simulation time, without seeing the future.
    assert not e.payload_blocked(51.)


def test_diagnostic_input_has_no_latent_event_or_seed_data():
    v = vehicle((main(),))
    delayed = engine("ascent_engine_out", 50., "booster_main_0")
    nominal = TroubleEngine(make_manifest(42, nominal_probability=1.))
    s = state(v)
    delayed.before_step(s, v, "ship", "stack_ascent")
    nominal.before_step(s, v, "ship", "stack_ascent")
    assert delayed.observation(s, v, "ship") == nominal.observation(s, v, "ship")
    text = json.dumps(delayed.observation(s, v, "ship"))
    for forbidden in ("seed", "family", "delay_s", "scheduled_at", "fault_", "manifest", "ascent_engine_out"):
        assert forbidden not in text
    changed = delayed.observation(s, v, "ship")
    changed["engine_health"][0]["available"] = False
    assert delayed.observation(s, v, "ship")["engine_health"][0]["available"]


def test_audit_hash_chain_binds_physical_effect_and_is_detached():
    e = engine("ascent_engine_out", 20., "booster_main_0")
    v = vehicle((main(),))
    e.before_step(state(v), v, "ship", "stack_ascent")
    e.before_step(state(v, 20.), v, "ship", "stack_ascent")
    audit = e.audit()
    row = audit["receipts"][0]
    digest = row.pop("receipt_sha256")
    assert row["previous_sha256"] == e.manifest_sha256
    assert sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest() == digest
    row["effects"].clear()
    assert e.audit()["receipts"][0]["effects"]


def test_missing_hardware_fails_visibly_and_does_not_claim_application():
    e, v = engine("ship_engine_out", 5., "ship_main_5"), vehicle((main("ship_main_0"),))
    e.before_step(state(v), v, "ship", "ship_ascent")
    with pytest.raises(ValueError, match="target_missing"):
        e.before_step(state(v, 5.), v, "ship", "ship_ascent")
    assert e.audit()["receipts"] == []
    assert e.audit()["unapplied_event_ids"] == ["fault_01"]


def test_booster_failure_uses_actual_availability_and_body_clocks_cannot_reverse():
    e, v = engine("booster_restart_failure", 0., "booster_main_0"), vehicle((main(),))
    s, _ = e.before_step(state(v, 300.), v, "booster", "booster_boostback")
    assert not s.engine_states[0].available
    with pytest.raises(ValueError, match="clock_reversed"):
        e.before_step(state(v, 299.), v, "booster", "booster_boostback")

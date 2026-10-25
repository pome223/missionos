"""Tiny real-RK4 fuel event checks, not missions, forecasts or hosted models.

Most tests advance a synthetic one-engine body for <=0.1 simulated seconds.
The network/process fixture forbids provider and full-worker launches; it does
not replace the production RK4/RHS. Separate synthetic error injection exercises
exact interrupted/failed receipt accounting and global-hook cleanup.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import math
import socket
import subprocess

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_restored_integration as integration


@pytest.fixture(autouse=True)
def no_provider_or_worker(monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("Tiny fuel-event test must not launch a provider/full worker")
    for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def fixture(*, fuel=.01, throttle=1., available=True, enabled=True, panel=False):
    engine = dyn.Engine("toy", (0., 0., 0.), 1000., 100., min_throttle=0.,
        throttle_time_constant_s=.4, gimbal_time_constant_s=.4)
    panels = (dyn.AeroPanel("toy_flap", (0., 0., 0.), (1., 0., 0.), 1.),) if panel else ()
    body = dyn.Vehicle6DOF(1000., (0., 0., 0.), ((10., 0., 0.), (0., 10., 0.), (0., 0., 10.)),
        10., (0., 0., 0.), 1., 1., engines=(engine,), aero_panels=panels)
    state = dyn.State6DOF(0., (env.EARTH_EQUATORIAL_RADIUS_M+1000., 0., 0.),
        (0., 0., 0.), dyn.IDENTITY, dyn.ZERO, fuel, (dyn.EngineState(throttle, 0., 0., available),),
        (0.,) if panel else ())
    command = dyn.Command6DOF((dyn.EngineCommand(enabled, 1. if enabled else 0.),),
        (.2,) if panel else ())
    return state, body, command


def advance(state, body, command, dt=.1):
    return dyn.step(state, body, command, dt, gravity=False, j2=False,
        atmosphere=False, gravity_gradient=False)


def v2(count=2):
    return integration.RestoredIntegrationPolicy(count, fuel_event_mode="outer_bracket_v2")


@pytest.mark.parametrize("count", [2, 4, 8])
@pytest.mark.parametrize("event_fraction", [.01, .25, .5, .999999, 1.])
def test_real_rk4_depletion_matches_rocket_equation_and_keeps_dry_actuators(count, event_fraction):
    # One axial engine through CoM, no external loads: exact integrated impulse
    # is ve*ln(m0/m1), independently of the event location inside the RK4 call.
    mass_rate = 1000./(100.*env.STANDARD_GRAVITY_MPS2)
    fuel = mass_rate*.1*event_fraction
    state, body, command = fixture(fuel=fuel)
    baseline = dyn._rk4
    with integration.restored_integration(v2(count)) as session:
        final = advance(state, body, command)
    assert dyn._rk4 is baseline and final.time_s == .1
    assert final.propellant_kg == 0. and final.engine_states[0].available is True
    assert final.v_eci_mps[:2] == (0., 0.) and final.omega_body_rad_s == dyn.ZERO
    expected = 100.*env.STANDARD_GRAVITY_MPS2*math.log1p(fuel/1000.)
    assert final.v_eci_mps[2] == pytest.approx(expected, abs=2e-9)
    assert 0. < final.engine_states[0].throttle <= 1.
    if event_fraction < 1.:
        assert final.engine_states[0].throttle < 1.  # finite dry decay; never reset
    observed = dyn.observe(final, body, command, gravity=False, atmosphere=False, gravity_gradient=False)
    assert observed["mass_flow_kg_s"] == 0.
    assert all(row["thrust_n"] == 0. for row in observed["engine_loads"])
    receipt = integration.validate_execution_receipt(session.record(), v2(count))
    trace = receipt["trace"]
    assert trace["rk4_substeps"]+trace["skipped_rk4_substeps"] == count*trace["rk4_calls"]
    assert trace["failed_rk4_calls"] == trace["incomplete_failure_substeps"] == 0
    if event_fraction == .01:
        assert trace["interrupted_rk4_calls"] > 0 and trace["skipped_rk4_substeps"] > 0


def test_real_strict_v1_still_refuses_the_original_inner_crossing():
    state, body, command = fixture()
    with pytest.raises(ValueError, match="numerics_subdivision_crossed_fuel_zero_unsupported"):
        with integration.restored_integration(integration.RestoredIntegrationPolicy()) as session:
            advance(state, body, command)
    assert session.record()["status"] == "failed"
    assert session.record()["schema"].endswith(".v1")


@pytest.mark.parametrize("count", [2, 4, 8])
@pytest.mark.parametrize("fuel,throttle,available", [(1., 1., True), (0., 1., True), (1., 1., False)])
def test_non_crossing_real_plant_preserves_v1_state_bits(count, fuel, throttle, available):
    state, body, command = fixture(fuel=fuel, throttle=throttle, available=available)
    with integration.restored_integration(integration.RestoredIntegrationPolicy(count)):
        prior = advance(state, body, command)
    with integration.restored_integration(v2(count)) as session:
        proposed = advance(state, body, command)
    assert asdict(prior) == asdict(proposed)
    trace = integration.validate_execution_receipt(session.record(), v2(count))["trace"]
    assert trace["interrupted_rk4_calls"] == trace["skipped_rk4_substeps"] == 0
    assert trace["rk4_substeps"] == count*trace["rk4_calls"]


def test_disabled_command_residual_throttle_depletes_then_gimbal_and_flap_continue():
    state, body, command = fixture(enabled=False, panel=True)
    command = replace(command, engines=(replace(command.engines[0], gimbal_x_rad=.1),))
    with integration.restored_integration(v2()) as session:
        final = advance(state, body, command)
    assert final.propellant_kg == 0. and final.time_s == .1
    assert 0. < final.engine_states[0].throttle < 1. and final.engine_states[0].available is True
    assert 0. < final.engine_states[0].gimbal_x_rad < .1
    assert 0. < final.flap_angles_rad[0] < .2
    assert env.norm(final.v_eci_mps) > 0.
    integration.validate_execution_receipt(session.record(), v2())


@pytest.mark.parametrize("fuel,available", [(0., True), (.01, False)])
def test_unavailable_or_dry_body_cannot_burn_saved_throttle(fuel, available):
    state, body, command = fixture(fuel=fuel, available=available)
    with integration.restored_integration(v2()) as session:
        final = advance(state, body, command)
    assert final.propellant_kg == fuel and final.v_eci_mps == dyn.ZERO
    assert 0. < final.engine_states[0].throttle < 1.
    assert final.engine_states[0].available is available
    integration.validate_execution_receipt(session.record(), v2())


@pytest.mark.parametrize("event_time", [1e-16, 7.5e-15])
def test_unrepresentable_initial_event_fails_finitely_and_restores_hook(event_time):
    rate = 1000./(100.*env.STANDARD_GRAVITY_MPS2)
    state, body, command = fixture(fuel=rate*event_time)
    original = dyn._rk4
    with pytest.raises(ValueError, match="refined_fuel_root_endpoint_not_representable"):
        with integration.restored_integration(v2()) as session:
            advance(state, body, command)
    assert dyn._rk4 is original and session.record()["status"] == "failed"
    assert session.record()["trace"]["rk4_calls"] < 100


def test_real_contact_after_depletion_is_a_fast_impact_not_a_fabricated_landing():
    from src.runtime.starship_sixdof_contact import find_contact
    state, body, command = fixture()
    state = replace(state, r_eci_m=(env.EARTH_EQUATORIAL_RADIUS_M+.5, 0., 0.), v_eci_mps=(-10., 0., 0.))
    with integration.restored_integration(v2()) as session:
        final, contact = find_contact(state, body, command, .1, .1, .01,
            gravity=False, j2=False, atmosphere=False)
    assert contact["contact"] is True and contact["initial_overlap"] is False
    assert 0.00980665 < final.time_s < .1 and final.propellant_kg == 0.
    assert contact["surface_normal_speed_mps"] < -9.9
    assert contact["surface_relative_speed_mps"] > 5.
    assert contact["landing_verified"] is contact["contact_response_modeled"] is False
    integration.validate_execution_receipt(session.record(), v2())


@pytest.mark.parametrize("count", [2, 4, 8])
def test_complete_policy_roundtrip_never_infers_v2(count):
    for policy in (integration.RestoredIntegrationPolicy(count), v2(count)):
        assert integration.RestoredIntegrationPolicy.from_record(policy.record()) == policy
    for partial in ({"subdivisions": count}, {**v2(count).record(), "fuel_zero_inside_inner_step": "clamp"}):
        with pytest.raises(ValueError):
            integration.RestoredIntegrationPolicy.from_record(partial)


def test_v2_receipt_cannot_relabel_v1_omit_skips_or_hide_failed_primitive():
    state, body, command = fixture()
    with integration.restored_integration(v2()) as session:
        advance(state, body, command)
    receipt = session.record()
    for mutation in ("schema", "policy", "skipped", "completed_primitive_failure"):
        forged = deepcopy(receipt)
        if mutation == "schema":
            forged["schema"] = "missionos.starship_restored_integration_execution.v1"
        elif mutation == "policy":
            forged["policy"] = integration.RestoredIntegrationPolicy().record()
        elif mutation == "skipped":
            forged["trace"]["skipped_rk4_substeps"] += 1
        else:
            forged["trace"]["failed_rk4_calls"] = 1
            forged["trace"]["incomplete_failure_substeps"] = 1
            forged["trace"]["rk4_substeps"] -= 1
        with pytest.raises(ValueError):
            integration.validate_execution_receipt(forged, v2())
    with pytest.raises(ValueError):
        integration.validate_execution_receipt(receipt, integration.RestoredIntegrationPolicy())


def test_primitive_error_is_failed_not_a_depletion_skip_and_hook_is_restored(monkeypatch):
    def broken(*_a):
        raise FloatingPointError("synthetic arithmetic failure, not a fuel bracket")
    broken.__module__, broken.__qualname__ = dyn.__name__, "_rk4"
    monkeypatch.setattr(integration, "_BASELINE_RK4", broken)
    monkeypatch.setattr(dyn, "_rk4", broken)
    state, body, command = fixture()
    with pytest.raises(FloatingPointError):
        with integration.restored_integration(v2(4)) as session:
            advance(state, body, command)
    assert dyn._rk4 is broken
    receipt = integration.validate_execution_receipt(session.record(), v2(4), require_completed=False)
    assert receipt["trace"]["failed_rk4_calls"] == 1
    assert receipt["trace"]["incomplete_failure_substeps"] == 4
    assert receipt["trace"]["skipped_rk4_substeps"] == receipt["trace"]["interrupted_rk4_calls"] == 0
    with pytest.raises(ValueError):
        integration.validate_execution_receipt(receipt, v2(4))


def test_typed_bracket_cannot_be_reassigned_to_another_trial():
    state, body, command = fixture()
    y, available = dyn._pack(state), (True,)
    event = dyn._FuelDepletionCrossing(y, body, command, available, .1, 0.,
        subdivisions=2, completed_substeps=1, fuel_before=.01, fuel_after=-.01)
    event.validate(y, body, command, available, .1, 0.)
    with pytest.raises(ValueError, match="not_current_trial"):
        event.validate(list(y), body, command, available, .1, 0.)


def test_real_short_trial_final_inner_negative_returns_typed_bracket_not_endpoint():
    # Direct real RK4 trial: final-inner-only depletion, even below step's outer
    # loop threshold. It must be a bracket, not a negative/zero sentinel state.
    rate = 1000./(100.*env.STANDARD_GRAVITY_MPS2)
    state, body, command = fixture(fuel=rate*9e-15)
    y, available = dyn._pack(state), (True,)
    with integration.restored_integration(v2()) as session:
        endpoint, crossing = dyn._fuel_trial(y, body, command, available, 1.5e-14,
            False, False, False, dyn.ZERO, dyn.ZERO, False, state.time_s)
    assert endpoint is None and crossing is True
    trace = integration.validate_execution_receipt(session.record(), v2())["trace"]
    assert trace["rk4_calls"] == trace["interrupted_rk4_calls"] == trace["final_endpoint_crossings"] == 1
    assert trace["rk4_substeps"] == 2 and trace["skipped_rk4_substeps"] == 0


def test_last_inner_dry_negative_is_failed_with_no_uncomputed_substeps(monkeypatch):
    calls = []
    def broken(y, *_a):
        calls.append(True)
        out = list(y)
        if len(calls) == 2:
            out[13] = -1e-20
        return out
    broken.__module__, broken.__qualname__ = dyn.__name__, "_rk4"
    monkeypatch.setattr(integration, "_BASELINE_RK4", broken)
    monkeypatch.setattr(dyn, "_rk4", broken)
    state, body, command = fixture(fuel=0.)
    with pytest.raises(ValueError, match="refined_dry_start_negative_fuel_invalid"):
        with integration.restored_integration(v2()) as session:
            advance(state, body, command)
    assert dyn._rk4 is broken
    trace = integration.validate_execution_receipt(session.record(), v2(), require_completed=False)["trace"]
    assert trace["rk4_calls"] == trace["failed_rk4_calls"] == trace["invalid_endpoint_rk4_calls"] == 1
    assert trace["rk4_substeps"] == 2 and trace["incomplete_failure_substeps"] == 0
    assert trace["interrupted_rk4_calls"] == trace["final_endpoint_crossings"] == trace["skipped_rk4_substeps"] == 0


def test_saved_sensor_wire_cannot_omit_or_swap_frozen_policy():
    from src.runtime import starship_final_sensor_execution as sensor
    from src.runtime.starship_final_verifier import _sensor_integration_binding
    legacy = integration.RestoredIntegrationPolicy(2).record()
    modern = v2().record()
    assert _sensor_integration_binding({"schema": sensor.SCHEMA}, {"integration_policy": legacy}) == {}
    assert _sensor_integration_binding({"schema": sensor.SCHEMA_V2, "integration_policy": modern},
        {"integration_policy": modern}) == {"integration_policy": modern}
    for record, expected in (({"schema": sensor.SCHEMA}, modern),
            ({"schema": sensor.SCHEMA, "integration_policy": modern}, modern),
            ({"schema": sensor.SCHEMA_V2, "integration_policy": legacy}, modern),
            ({"schema": sensor.SCHEMA_V2}, modern)):
        with pytest.raises(ValueError):
            _sensor_integration_binding(record, {"integration_policy": expected})


def test_changed_plant_hash_cannot_borrow_historic_source_equivalence():
    from hashlib import sha256
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    raw = (root/"tests/fixtures/starship/restored-numerics-diagnostic-source.py.txt").read_bytes()
    prior = {f"src/runtime/starship_physical_{i}.py": f"{i:064x}" for i in range(186)}
    prior["src/runtime/starship_sixdof.py"] = "a"*64
    current = {**prior, "src/runtime/starship_sixdof.py": "b"*64}
    with pytest.raises(ValueError):
        integration.integration_source_equivalence(raw, expected_diagnostic_sha256=sha256(raw).hexdigest(),
            prior_physical_sources=prior, current_physical_sources=current)

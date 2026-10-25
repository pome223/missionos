"""Synthetic held-command arithmetic and source checks; no plant/provider runs."""
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
import socket
import subprocess
import threading

import numpy as np
import pytest

from src.runtime import starship_restored_integration as integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_real_dynamics_or_network(monkeypatch):
    from src.runtime import starship_physics as env
    def forbidden(*_a, **_kw):
        raise AssertionError("Integration contracts must not execute dynamics/providers")
    for owner, name in ((integration.dyn, "step"), (integration.dyn, "_rhs"), (env, "step"),
            (socket, "create_connection"), (socket, "getaddrinfo"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def install(monkeypatch, implementation):
    # The synthetic dependency is deliberately injected at both bindings; real
    # activation accepts only the unchanged imported plant function identity.
    implementation.__module__ = integration.dyn.__name__
    implementation.__qualname__ = "_rk4"
    monkeypatch.setattr(integration, "_BASELINE_RK4", implementation)
    monkeypatch.setattr(integration.dyn, "_rk4", implementation)


def arguments(fuel=100.):
    y = np.arange(18, dtype=float)
    y[13] = fuel
    return (y, object(), object(), (True, False), .25, True, False, True, object(), object(), False, 123.5)


def test_policy_is_immutable_explicit_and_default_is_two_without_installing():
    original = integration.dyn._rk4
    policy = integration.RestoredIntegrationPolicy()
    assert policy.subdivisions == 2 and integration.dyn._rk4 is original
    with pytest.raises(FrozenInstanceError):
        policy.subdivisions = 4
    record = policy.record()
    assert record["experimental"] is True
    assert record["qualification_refreshed"] is False and record["mission_authority_granted"] is False
    record["subdivisions"] = 8
    assert policy.record()["subdivisions"] == 2


@pytest.mark.parametrize("value", [True, False, 1, 0, 3, -2, 2., "4", None, float("nan")])
def test_invalid_subdivision_never_installs(value):
    original = integration.dyn._rk4
    with pytest.raises(ValueError, match="invalid_subdivision"):
        integration.RestoredIntegrationPolicy(value)
    assert integration.dyn._rk4 is original


@pytest.mark.parametrize("count", [2, 4, 8])
def test_held_actual_inputs_absolute_times_and_completed_counts(monkeypatch, count):
    calls = []
    args = arguments()
    def fake(y, *rest):
        calls.append((y.copy(), *rest))
        out = y.copy()
        out[0] += rest[3]
        return out
    install(monkeypatch, fake)
    with integration.restored_integration(integration.RestoredIntegrationPolicy(count)) as session:
        out = integration.dyn._rk4(*args)
        assert session.record()["status"] == "active"
        snapshot = session.record()
        snapshot["trace"]["rk4_substeps"] = -1
    assert integration.dyn._rk4 is fake
    assert args[0][0] == 0. and out[0] == pytest.approx(.25)
    assert len(calls) == count
    for index, call in enumerate(calls):
        assert all(call[position] is args[position] for position in (1, 2, 3, 8, 9))
        assert call[4] == args[4]/count and call[-1] == args[-1]+index*args[4]/count
        assert call[5:8] == args[5:8] and call[10] is args[10]
    record = session.record()
    assert record["status"] == "completed"
    assert record["trace"] == {"rk4_calls": 1, "rk4_substeps": count,
        "commands_recomputed_inside_rk4": 0, "outer_control_cadence_changed": False,
        "fuel_event_search_changed": False, "contact_search_changed": False}
    assert record["hosted_model_calls"] == record["nested_sixdof_forecasts"] == 0
    assert record["execution_permission_granted"] is record["qualification_refreshed"] is False


@pytest.mark.parametrize("count", [2, 4, 8])
def test_zero_to_zero_inert_body_is_valid(monkeypatch, count):
    def fake(y, *_rest):
        assert y[13] == 0.
        return y.copy()
    install(monkeypatch, fake)
    with integration.restored_integration(integration.RestoredIntegrationPolicy(count)) as session:
        result = integration.dyn._rk4(*arguments(0.))
    assert result[13] == 0. and session.record()["trace"]["rk4_substeps"] == count


@pytest.mark.parametrize("start,end", [(1., 0.), (1., -1e-9), (0., -1e-9)])
def test_inner_depletion_fails_and_partial_trace_is_retained(monkeypatch, start, end):
    def fake(y, *_rest):
        out = y.copy()
        out[13] = end
        return out
    install(monkeypatch, fake)
    with pytest.raises(ValueError, match="crossed_fuel_zero_unsupported"):
        with integration.restored_integration(integration.RestoredIntegrationPolicy(4)) as session:
            integration.dyn._rk4(*arguments(start))
    assert integration.dyn._rk4 is fake
    record = session.record()
    assert record["status"] == "failed" and record["failure_class"] == "ValueError"
    assert record["trace"]["rk4_calls"] == record["trace"]["rk4_substeps"] == 1


def test_final_fuel_endpoint_remains_outer_event_search_responsibility(monkeypatch):
    calls = []
    def fake(y, *_rest):
        calls.append(True)
        out = y.copy()
        out[13] = 1. if len(calls) == 1 else -1.
        return out
    install(monkeypatch, fake)
    with integration.restored_integration(integration.RestoredIntegrationPolicy()) as session:
        out = integration.dyn._rk4(*arguments())
    assert out[13] == -1. and session.record()["status"] == "completed"


def test_original_step_exception_and_body_exception_restore_hook_and_release_lock(monkeypatch):
    def fake(*_args):
        raise TimeoutError("synthetic operation")
    install(monkeypatch, fake)
    for operation in (lambda: integration.dyn._rk4(*arguments()), lambda: (_ for _ in ()).throw(KeyboardInterrupt())):
        with pytest.raises((TimeoutError, KeyboardInterrupt)):
            with integration.restored_integration(integration.RestoredIntegrationPolicy()) as session:
                operation()
        assert integration.dyn._rk4 is fake and session.record()["status"] == "failed"
    with integration.restored_integration(integration.RestoredIntegrationPolicy()) as session:
        pass
    assert session.record()["status"] == "completed"


def test_nested_and_concurrent_hook_owners_fail_immediately(monkeypatch):
    install(monkeypatch, lambda *_a: None)
    failures = []
    def another_thread():
        try:
            with integration.restored_integration(integration.RestoredIntegrationPolicy()):
                pytest.fail("concurrent hook entered")
        except ValueError as error:
            failures.append(str(error))
    with integration.restored_integration(integration.RestoredIntegrationPolicy()):
        with pytest.raises(ValueError, match="hook_already_active"):
            with integration.restored_integration(integration.RestoredIntegrationPolicy(4)):
                pytest.fail("nested hook entered")
        thread = threading.Thread(target=another_thread)
        thread.start()
        thread.join(timeout=1.)
        assert not thread.is_alive()
    assert failures == ["restored_integration_hook_already_active"]


def test_foreign_hook_refused_and_never_removed(monkeypatch):
    original = integration.dyn._rk4
    def foreign(*_a):
        return None
    monkeypatch.setattr(integration.dyn, "_rk4", foreign)
    with pytest.raises(ValueError, match="foreign_hook_active"):
        with integration.restored_integration(integration.RestoredIntegrationPolicy()):
            pytest.fail("foreign hook accepted")
    assert integration.dyn._rk4 is foreign
    monkeypatch.setattr(integration.dyn, "_rk4", original)
    with integration.restored_integration(integration.RestoredIntegrationPolicy()):
        pass


def test_replacement_during_scope_is_detected_restored_and_reusable(monkeypatch):
    original = integration.dyn._rk4
    try:
        with pytest.raises(ValueError, match="replaced_during_scope"):
            with integration.restored_integration(integration.RestoredIntegrationPolicy()) as session:
                # The production scope must observe and restore this foreign
                # assignment. MonkeyPatch would remember the active closure
                # here and reinstall it later during fixture teardown.
                integration.dyn._rk4 = lambda *_a: None
        assert integration.dyn._rk4 is original and session.record()["status"] == "failed"
        with integration.restored_integration(integration.RestoredIntegrationPolicy()):
            pass
        # Exercise fixture undo before this test ends: neither pytest teardown
        # nor the runtime may leave an active refinement closure installed.
        monkeypatch.undo()
        assert integration.dyn._rk4 is original
    finally:
        # Keep a failed assertion from contaminating unrelated plant tests.
        integration.dyn._rk4 = original


def test_forked_import_and_untyped_policy_refused(monkeypatch):
    with pytest.raises(ValueError, match="typed_policy_required"):
        with integration.restored_integration({"subdivisions": 2}):
            pytest.fail("untyped policy accepted")
    monkeypatch.setattr(integration, "_PROCESS_ID", -1)
    with pytest.raises(ValueError, match="fresh_import"):
        with integration.restored_integration(integration.RestoredIntegrationPolicy()):
            pytest.fail("forked hook accepted")


def source_fixture():
    raw = (ROOT/"tests/fixtures/starship/restored-numerics-diagnostic-source.py.txt").read_bytes()
    prior = {f"src/runtime/starship_physical_{i}.py": f"{i:064x}" for i in range(187)}
    return raw, {"expected_diagnostic_sha256": sha256(raw).hexdigest(),
        "prior_physical_sources": prior, "current_physical_sources": dict(prior)}


def test_exact_diagnostic_algorithm_ast_and_all_187_pins_are_reuse_prerequisite_only():
    raw, options = source_fixture()
    options["current_physical_sources"]["src/runtime/starship_restored_integration.py"] = "f"*64
    proof = integration.integration_source_equivalence(raw, **options)
    assert proof["algorithm_ast_identical"] is True and proof["physical_source_files_unchanged"] == 187
    assert proof["additional_runtime_source_files"] == 1
    assert all(proof[key] is False for key in ("profile_or_environment_equivalence_checked",
        "prior_record_validated", "evidence_reuse_authorized", "numerical_gate_passed",
        "qualification_refreshed", "mission_authority_granted"))
    assert len(proof["algorithm_ast_sha256"]) == 64


@pytest.mark.parametrize("mutation", ["diagnostic_pin", "different_loop", "old_bad_fuel_guard", "missing",
    "changed", "old_count", "unsafe", "invalid_digest", "absent_algorithm", "syntax"])
def test_equivalence_rejects_algorithm_physics_inventory_and_source_drift(mutation):
    raw, options = source_fixture()
    if mutation == "diagnostic_pin":
        options["expected_diagnostic_sha256"] = "0"*64
    elif mutation == "different_loop":
        raw = raw.replace(b"dt/subdivisions", b"dt/(subdivisions+1)")
    elif mutation == "old_bad_fuel_guard":
        raw = raw.replace(b"(out[13] < 0 or before_fuel > 0 and out[13] == 0)", b"out[13] <= 0")
    elif mutation == "missing":
        options["current_physical_sources"].pop("src/runtime/starship_physical_0.py")
    elif mutation == "changed":
        options["current_physical_sources"]["src/runtime/starship_physical_0.py"] = "e"*64
    elif mutation == "old_count":
        options["prior_physical_sources"].pop("src/runtime/starship_physical_0.py")
    elif mutation == "unsafe":
        options["current_physical_sources"]["../src/runtime/starship_private.py"] = "e"*64
    elif mutation == "invalid_digest":
        options["prior_physical_sources"]["src/runtime/starship_physical_0.py"] = True
    elif mutation == "absent_algorithm":
        raw = raw.replace(b"def refined(", b"def other(")
    else:
        raw = b"not python @!"
    if mutation != "diagnostic_pin":
        options["expected_diagnostic_sha256"] = sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        integration.integration_source_equivalence(raw, **options)

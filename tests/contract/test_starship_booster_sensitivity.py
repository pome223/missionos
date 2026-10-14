"""Bounds and negative-result preservation for the offline booster probe.

These tests use no physical simulation or provider invocation. The authorized
three-case CLI run separately exercises the actual six-DOF boundary.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from scripts import probe_starship_booster_sensitivity as probe


def cli(output, study=None):
    return ["--study", str(study or output.parent/"source.json"), "--study-sha256", "a"*64,
            "--output-dir", str(output), "--approve-simulation"]


def test_opt_in_and_fresh_output_are_required_before_read_or_simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "read_origin", lambda *args: pytest.fail("input must not be read"))
    with pytest.raises(SystemExit) as error:
        probe.main(cli(tmp_path/"new")[:-1])
    assert error.value.code == 2
    assert not (tmp_path/"new").exists()
    with pytest.raises(SystemExit) as error:
        probe.main(cli(tmp_path))
    assert error.value.code == 2


def test_source_hash_is_required_before_parsing(tmp_path):
    p = tmp_path/"source.json"
    p.write_text("not JSON")
    with pytest.raises(ValueError, match="input_study_sha256_mismatch"):
        probe.read_origin(p, "a"*64)


def test_nonprefix_input_cannot_be_relabelled_as_current_m3(tmp_path):
    p = tmp_path/"source.json"
    p.write_text('{"schema":"invented"}')
    with pytest.raises(ValueError, match="exact_m3_prefix_study_required"):
        probe.read_origin(p, probe.sha256(p.read_bytes()).hexdigest())


@pytest.mark.parametrize("delta", [-1000., 0., 1000.])
def test_only_fuel_is_changed_without_mutating_or_resetting_origin(delta):
    origin = {"time_s": 135.6, "propellant_kg": 260000., "r_eci_m": [1., 2., 3.],
        "engine_states": [{"available": False, "throttle": .4}], "flap_angles_rad": [.1]}
    before = deepcopy(origin)
    changed = probe.perturbed_state(origin, delta)
    assert changed["propellant_kg"] == before["propellant_kg"]+delta
    assert {k:v for k,v in changed.items() if k != "propellant_kg"} == {k:v for k,v in before.items() if k != "propellant_kg"}
    changed["engine_states"][0]["available"] = True
    assert origin == before


@pytest.mark.parametrize("delta", [1., -2000., float("nan"), True])
def test_unregistered_offsets_rejected(delta):
    with pytest.raises(ValueError, match="fuel_delta_not_registered"):
        probe.perturbed_state({"propellant_kg": 260000.}, delta)


def test_nominal_match_covers_actual_commands_and_state_but_not_ai_metadata():
    run = {key: {} for key in probe.PHYSICAL_FIELDS}
    run["samples"] = [{"command": {"throttle": .4}, "r_eci_m": [1, 2, 3]}]
    original = probe.physical_digest(run)
    run["shared_clock_checkpoint"] = "different offline scheduling receipt"
    run["events"] = [{"event": "managed_booster_command"}]
    assert probe.physical_digest(run) == original
    run["samples"][0]["command"]["throttle"] = .5
    assert probe.physical_digest(run) != original


def setup_campaign(monkeypatch):
    origin = {"profile": {"booster_return": {"max_duration_s": 1200.}}, "goal": {"fixed": True}}
    monkeypatch.setattr(probe, "read_origin", lambda *a: deepcopy(origin))
    monkeypatch.setattr(probe, "source_hashes", lambda: {"source.py": "b"*64})
    monkeypatch.setattr(probe, "backend", lambda: {"python": "fixture", "numpy": "fixture", "scipy": "fixture"})
    return origin


def test_physical_failure_preserved_without_changing_fixed_remaining_cases(tmp_path, monkeypatch):
    setup_campaign(monkeypatch)
    calls = []
    def fake_case(output, name, protocol_sha, origin_sha):
        protocol = json.loads((output/"protocol.json").read_text())
        assert protocol["cases"] == [{"case_id": n, "delta_fuel_kg": d} for n,d in probe.CASES]
        assert probe.digest(protocol) == protocol_sha
        calls.append(name)
        (output/name).mkdir()
        probe.write_new(output/name/"summary.json", {"case_id": name,
            "record_verification": {"passed": True},
            "nominal_physics_exactly_reproduced": True if name == "nominal" else None,
            "controlled_water_entry_envelope_met": name == "nominal"})
        return {"exit_code": 0, "interruption": None}
    monkeypatch.setattr(probe, "run_bounded", fake_case)
    output = tmp_path/"new"
    assert probe.main(cli(output)) == 0
    summary = json.loads((output/"summary.json").read_text())
    assert calls == [n for n,_ in probe.CASES]
    assert summary["all_cases_executed_and_record_verified"] is True
    assert summary["all_three_entry_envelopes_met"] is False
    assert summary["retry_performed"] is False
    assert summary["ai_value_assessed"] is False
    assert summary["perturbation_qualification_claimed"] is False


def test_nominal_nonreproduction_stops_before_any_perturbation(tmp_path, monkeypatch):
    setup_campaign(monkeypatch)
    calls = []
    def mismatch(output, name, *args):
        calls.append(name)
        (output/name).mkdir()
        probe.write_new(output/name/"summary.json", {"record_verification": {"passed": True},
            "nominal_physics_exactly_reproduced": False, "controlled_water_entry_envelope_met": True})
        return {"exit_code": 0, "interruption": None}
    monkeypatch.setattr(probe, "run_bounded", mismatch)
    output = tmp_path/"new"
    assert probe.main(cli(output)) == 2
    assert calls == ["nominal"]
    summary = json.loads((output/"summary.json").read_text())
    assert summary["execution_error"]["message"] == "nominal_physics_did_not_reproduce"
    assert not summary["all_cases_executed_and_record_verified"]
    assert not (output/"minus_1000kg").exists()


def test_source_change_after_protocol_prevents_execution(tmp_path, monkeypatch):
    setup_campaign(monkeypatch)
    hashes = iter(({"source.py": "old"}, {"source.py": "new"}))
    monkeypatch.setattr(probe, "source_hashes", lambda: next(hashes))
    monkeypatch.setattr(probe, "run_bounded", lambda *a: pytest.fail("must reject before starting worker"))
    output = tmp_path/"new"
    assert probe.main(cli(output)) == 2
    summary = json.loads((output/"summary.json").read_text())
    assert summary["execution_error"]["message"] == "source_or_backend_changed"
    assert summary["cases"] == []


@pytest.mark.parametrize("cause", ["wall", "storage"])
def test_worker_is_terminated_at_frozen_resource_bound(tmp_path, monkeypatch, cause):
    class Child:
        alive = True
        exitcode = None
        pid = 123
        def start(self):
            pass
        def is_alive(self):
            return self.alive
        def terminate(self):
            self.alive = False
            self.exitcode = -15
        def join(self, timeout=None):
            pass
        def kill(self):
            pytest.fail("graceful termination already finished")
    child = Child()
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda *a: SimpleNamespace(free=probe.MIN_FREE_BYTES))
    class Context:
        def Process(self, **kwargs):
            return child
    monkeypatch.setattr(probe.multiprocessing, "get_context", lambda *a: Context())
    ticks = iter((0., 601., 602.)) if cause == "wall" else iter((0., 1., 2.))
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probe, "directory_bytes", lambda *a: 200_000_001 if cause == "storage" else 0)
    result = probe.run_bounded(tmp_path, "nominal", "a"*64, "b"*64)
    assert result["interruption"] == ("case_wall_time_cap" if cause == "wall" else "campaign_output_cap")
    assert child.alive is False
    assert result["retry_performed"] is False
    assert (tmp_path/"nominal/attempt.json").exists()


def test_low_space_prevents_child_creation_without_deleting_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda *a: SimpleNamespace(free=probe.MIN_FREE_BYTES-1))
    monkeypatch.setattr(probe.multiprocessing, "get_context", lambda *a: pytest.fail("no worker permitted"))
    kept = tmp_path/"earlier.json"
    kept.write_text("preserved")
    with pytest.raises(ValueError, match="minimum_free_space_before_case"):
        probe.run_bounded(tmp_path, "plus_1000kg", "a"*64, "b"*64)
    assert kept.read_text() == "preserved"
    assert not (tmp_path/"plus_1000kg").exists()

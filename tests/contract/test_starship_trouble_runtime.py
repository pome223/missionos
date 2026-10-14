"""Small real 6DOF runs exercise fault hooks, not a mocked physics adapter."""
import json
from pathlib import Path

import pytest

from src.runtime.starship_sixdof_mission import simulate, simulate_concurrent
from src.runtime.starship_trouble_engine import fixture_manifest


@pytest.fixture(scope="module")
def profile():
    return json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())


def test_engine_loss_changes_motion_only_after_trigger(profile):
    normal = simulate_concurrent(profile, duration_s=25.)
    manifest = fixture_manifest([{"family":"ascent_engine_out", "delay_s":20., "target":"booster_main_0"}])
    faulted = simulate_concurrent(profile, duration_s=25., trouble_manifest=manifest)
    def before(run):
        return [(s["time_s"],s["r_eci_m"],s["v_eci_mps"],s["q_body_to_eci"],s["propellant_kg"])
                for s in run["samples"] if s["time_s"] < 20.]
    assert before(normal) == before(faulted)
    assert normal["final_state"]["r_eci_m"] != faulted["final_state"]["r_eci_m"]
    assert normal["final_state"]["propellant_kg"] != faulted["final_state"]["propellant_kg"]
    assert faulted["trouble"]["receipts"][0]["applied_at_s"] == 20.
    assert faulted["final_state"]["engine_states"][0]["available"] is False
    assert faulted["samples"][-2]["main_engine_thrust_n"][0] == 0.
    health = faulted["samples"][0]["controller"]["component_health"]
    assert all(row["available"] for row in health["engine_health"])
    assert not ({"seed","events","target","family","manifest"} & set(health))


def test_fault_survives_separation_and_booster_continues(profile):
    manifest = fixture_manifest([{"family":"ascent_engine_out", "delay_s":20., "target":"booster_main_0"},
                                 {"family":"tower_unavailable", "delay_s":0., "target":"tower"}])
    run = simulate_concurrent(profile, duration_s=170., trouble_manifest=manifest)
    assert run["booster_separation_state"]["engine_states"][0]["available"] is False
    assert run["booster_run"]["final_state"]["engine_states"][0]["available"] is False
    assert run["final_state"]["engine_states"][0]["available"] is True  # independent Ship engine
    assert run["booster_run"]["samples"][0]["controller"]["component_health"]["tower_ready"] is False
    assert len(run["trouble"]["receipts"]) == 2


def test_unreached_phase_is_not_forced_to_occur(profile):
    manifest = fixture_manifest([{"family":"payload_jam", "delay_s":0., "target":"dispenser"}])
    run = simulate_concurrent(profile, duration_s=1., trouble_manifest=manifest)
    assert run["trouble"]["receipts"] == []
    assert run["trouble"]["unapplied_event_ids"] == ["fault_01"]


def test_failed_ascent_is_integrated_without_inventing_an_orbit(profile):
    altered = json.loads(json.dumps(profile))
    altered["guidance"]["max_ascent_s"] = 140.
    manifest = fixture_manifest([])
    run = simulate_concurrent(altered, duration_s=150., trouble_manifest=manifest)
    assert run["outcome"]["orbit_gate_reached"] is False
    assert run["outcome"]["phase"] == "ballistic_return"
    assert run["final_state"]["time_s"] == pytest.approx(150.)
    assert any(e["event"] == "orbit_not_reached" for e in run["events"])


def test_fault_manifest_does_not_reuse_existing_qualified_return_grant(profile):
    with pytest.raises(ValueError, match="unqualified_standalone"):
        simulate_concurrent(profile, duration_s=1., trouble_manifest=fixture_manifest([]),
                            return_policy="trimmed_state_terminal_v4")


@pytest.mark.parametrize("options", [
    {"mission_case": "fuel_shortage"}, {"mission_case": "release_fault"},
    {"deployment_permission": lambda _: True}, {"return_sites": {}},
    {"catch_config": {}},
])
def test_fault_manifest_rejects_unrecorded_legacy_inputs(profile, options):
    with pytest.raises(ValueError, match="unqualified_standalone"):
        simulate_concurrent(profile, duration_s=1., trouble_manifest=fixture_manifest([]), **options)


def test_faults_require_shared_clock_before_any_integration(profile):
    with pytest.raises(ValueError, match="trouble_trials_require_concurrent_api"):
        simulate(profile, duration_s=1., trouble_manifest=fixture_manifest([]))

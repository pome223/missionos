"""The capture adapter uses real director dispatch and later telemetry."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from src.intelligence.starship_mission_director import fixture_decision
from src.runtime.starship_deployment_forecast import ObservedDeploymentDirector, authority
from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_mission_director import digest
from src.runtime.starship_return_feasibility import readiness
from src.runtime import starship_deployment_forecast as runtime
from tests.contract.test_starship_mission_director import row

ROOT = Path(__file__).resolve().parents[2]


def make_director(case="normal", **kwargs):
    approved = authority(case)
    tracker = DeploymentTracker(26, digest(approved))
    return ObservedDeploymentDirector(approved["deployment_authority"], tracker,
                                     fixture_decider=fixture_decision, **kwargs), tracker


def test_director_command_invalidates_before_following_plant_observation():
    director, tracker = make_director()
    # confirm() is invoked by both the flight loop and update().
    director.confirm("ship", row())
    before = tracker.generation
    assert director.update("deployment_start", row()) == "continue"
    assert tracker.generation == before + 1
    assert tracker.history[-1] == {"kind": "command", "action": "continue", "time_s": 100.0}
    assert director.records[0]["later_observation"] is None
    director.confirm("ship", row(102.0))
    assert director.records[0]["later_observation"]["time_s"] == 102.0
    assert tracker.history[-1]["released_count"] == 0


def test_ack_does_not_become_a_release_and_booster_does_not_move_ship_clock():
    director, tracker = make_director("release_fault")
    director.confirm("ship", row())
    director.confirm("ship", row(102.0, release_acknowledged=True, sequencer_state="inhibited"))
    before = deepcopy(tracker.history)
    director.confirm("booster", row(50.0, phase="booster_return"))
    assert tracker.history == before
    assert tracker.history[-1]["released_count"] == 0


def test_notice_stop_is_later_confirmed_without_changing_director_rules():
    director, tracker = make_director("operations_notice")
    director.confirm("ship", row())
    notice = row(102.0, released_count=1,
                 operations_notice="Payload operations: suspend remaining deployment")
    assert director.update("deployment_monitor", notice) == "stop_deployment"
    assert tracker.history[-1]["kind"] == "command"
    director.confirm("ship", row(104.0, released_count=1, sequencer_state="skipped"))
    assert director.records[0]["later_observation"]["sequencer_state"] == "skipped"
    assert tracker.history[-1]["released_count"] == 1
    assert director.records[0]["response"]["model_inference_invoked"] is False


def test_authority_is_fixed_fixture_only_and_physical_certificate_still_matches():
    with pytest.raises(ValueError, match="unknown_m2_handoff_fixture"):
        authority("live")
    grant = authority("normal")
    assert grant["maximum_model_calls"] == 0
    assert grant["ship_return_dispatch_authorized"] is False
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    assert readiness(profile, check_backend=False)[2] is None


def test_failed_producer_preserves_observation_prefix(monkeypatch, tmp_path):
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    monkeypatch.setattr(runtime, "readiness", lambda _: (None, None, None))

    def failed_producer(_profile, *, mission_director, return_sites, **_kwargs):
        # The inherited booster branch must have a concrete diversion site;
        # otherwise the real producer fails after finishing the Ship segment.
        assert return_sites.site("divert") is not None
        mission_director.confirm("ship", row())
        raise RuntimeError("injected_producer_failure")

    monkeypatch.setattr(runtime, "simulate", failed_producer)
    with pytest.raises(RuntimeError, match="injected_producer_failure"):
        runtime.run_fixture(profile, "normal", tmp_path)
    saved = json.loads((tmp_path / "observation-trace.json").read_text())
    assert saved == [{"kind": "observation", "row": row()}]
    assert not (tmp_path / "forecast.json").exists()


def test_cli_requires_explicit_simulation_opt_in_before_creating_output(tmp_path):
    output = tmp_path / "not-created"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_starship_m2_handoff.py"),
         "--case", "normal", "--output-dir", str(output)],
        cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 2
    assert "explicit --approve-simulation required" in result.stderr
    assert not output.exists()

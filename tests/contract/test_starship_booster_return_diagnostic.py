"""Input bindings and finite-resource scope for the offline return diagnostic."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from scripts.study_starship_booster_return import main
from src.runtime.starship_booster_catch import _initialized

ROOT = Path(__file__).resolve().parents[2]


def invocation(tmp_path, *, tiny_dt=False):
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    _, state = _initialized(profile, catch, "booster_catch")
    if tiny_dt:
        profile["integration"]["powered_dt_s"] = 1e-300
    raw_profile = json.dumps(profile).encode()
    study = {"profile": profile, "provenance": {"profile_sha256": hashlib.sha256(raw_profile).hexdigest()},
             "runs": [{"scenario": "recorded", "booster_separation_state": asdict(state)}]}
    raw_study = json.dumps(study).encode()
    (tmp_path/"profile.json").write_bytes(raw_profile)
    (tmp_path/"study.json").write_bytes(raw_study)
    return ["--study", str(tmp_path/"study.json"), "--study-sha256", hashlib.sha256(raw_study).hexdigest(),
            "--profile", str(tmp_path/"profile.json"), "--scenario", "recorded", "--policy", "site_return_v3",
            "--duration-s", ".1", "--output", str(tmp_path/"result"), "--approve-simulation"]


def test_real_cli_continues_bound_state_and_records_requested_horizon(tmp_path):
    args = invocation(tmp_path)
    assert main(args) == 0
    result = json.loads((tmp_path/"result/result.json").read_text())
    assert result["provenance"]["requested_duration_s"] == .1
    assert result["provenance"]["resolved_duration_s"] == .1
    assert result["run"]["initial_state"]["time_s"] == 600.
    assert result["run"]["final_state"]["time_s"] == pytest.approx(600.1)
    assert not result["catch_handoff_attempted"]
    assert not result["operational_policy_approved"]


@pytest.mark.parametrize("flag,value", [("--study-sha256", "0"*64), ("--scenario", "wrong"),
                                       ("--duration-s", "nan"), ("--duration-s", "1e-300")])
def test_invalid_bindings_or_clock_are_rejected_before_output_creation(tmp_path, flag, value):
    args = invocation(tmp_path)
    args[args.index(flag)+1] = value
    with pytest.raises(SystemExit):
        main(args)
    assert not (tmp_path/"result").exists()


def test_tiny_profile_time_step_is_rejected_even_with_exact_matching_hashes(tmp_path):
    with pytest.raises(SystemExit):
        main(invocation(tmp_path, tiny_dt=True))
    assert not (tmp_path/"result").exists()


def test_profile_edit_after_study_does_not_change_simulation_parameters(tmp_path):
    args = invocation(tmp_path)
    profile = json.loads((tmp_path/"profile.json").read_text())
    profile["booster"]["dry_mass_kg"] += 1000
    (tmp_path/"profile.json").write_text(json.dumps(profile))
    with pytest.raises(SystemExit):
        main(args)
    assert not (tmp_path/"result").exists()


def test_authorization_and_fresh_directory_are_required(tmp_path):
    args = invocation(tmp_path)
    with pytest.raises(SystemExit):
        main(args[:-1])
    (tmp_path/"result").mkdir()
    (tmp_path/"result/failure.json").write_text("preserved")
    with pytest.raises(SystemExit):
        main(args)
    assert (tmp_path/"result/failure.json").read_text() == "preserved"

"""Evidence failures must not become a real-vehicle or completed-mission claim."""

from copy import deepcopy
from dataclasses import replace
import json
import subprocess
import sys

import pytest

from src.runtime.starship_flight import FlightProfile, simulate_flight
from src.runtime.starship_study import (
    OBSERVATIONS,
    ROOT,
    compare_observations,
    make_request,
    run_study,
    study_content_hash,
    verify_flight,
    verify_study,
)


@pytest.fixture(scope="module")
def short_run():
    profile = replace(FlightProfile(), max_mission_duration_s=30.0)
    request = make_request(["flight14_inspired"], profile, 1.0, 5.0)
    return simulate_flight(profile), request


def test_numerically_consistent_incomplete_run_is_not_success(short_run):
    run, request = short_run
    verdict = verify_flight(run, request)
    assert verdict["evidence_verified"], verdict["reasons"]
    assert not verdict["payload_orbit_verified"]
    assert not verdict["ship_soft_contact_verified"]
    assert not verdict["simulated_mission_completed"]
    assert not verdict["starship_vehicle_validated"]


@pytest.mark.parametrize(
    "mutation",
    ["position", "speed", "vehicle", "control", "gravity", "truncate", "outcome", "teleport"],
)
def test_tampering_fails_frozen_model_or_numeric_verification(short_run, mutation):
    original, request = short_run
    run = deepcopy(original)
    record = run["step_records"]["ship"][5]
    if mutation == "position":
        record["after"]["r"][0] += 100.0
    elif mutation == "speed":
        run["traces"]["ship"][-1]["speed_mps"] = 0.0
    elif mutation == "vehicle":
        record["vehicle"]["max_thrust_n"] *= 2
    elif mutation == "control":
        record["control"]["throttle"] = 0.0
    elif mutation == "gravity":
        record["gravity"] = False
    elif mutation == "truncate":
        run["traces"]["ship"].pop()
    elif mutation == "outcome":
        run["outcomes"]["mission_completed"] = True
    elif mutation == "teleport":
        run["state_transitions"].append(
            {
                "body_id": "ship",
                "reason": "teleport",
                "before": record["before"],
                "after": record["after"],
            }
        )
    verdict = verify_flight(run, request)
    assert not verdict["evidence_verified"]
    assert not verdict["bounded_orbit_and_contact_verified"]


def test_no_extrapolation_or_missing_observations_as_zero_error(short_run):
    comparison = compare_observations(short_run[0], json.loads(OBSERVATIONS.read_text()))
    assert comparison["reference_altitude_points"] == 18
    assert comparison["covered_altitude_points"] == 2
    assert "V11" in comparison["missing_altitude_point_ids"]
    assert not comparison["accuracy_verified"]
    assert all("altitude_difference_m" not in r for r in comparison["rows"] if not r["covered"])


def test_opt_in_and_preservation(tmp_path):
    destination = tmp_path / "absent"
    with pytest.raises(PermissionError):
        run_study(destination)
    assert not destination.exists()
    destination.mkdir()
    (destination / "previous.json").write_text("preserve")
    with pytest.raises(ValueError, match="earlier results"):
        run_study(destination, approved=True)
    assert (destination / "previous.json").read_text() == "preserve"


@pytest.mark.parametrize("dt", [0, 3, True, float("nan")])
def test_reject_invalid_request_before_dispatch(dt):
    with pytest.raises(ValueError):
        make_request(["flight14_inspired"], FlightProfile(), dt, 5.0)


def test_real_cli_subprocess_receipt_and_offline_verifier(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text('{"max_mission_duration_s":30}')
    out = tmp_path / "run"
    command = [
        sys.executable,
        str(ROOT / "scripts/run_starship_3d.py"),
        "--approve-simulation",
        "--profile",
        str(profile),
        "--output-dir",
        str(out),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    study = json.loads((out / "study.json").read_text())
    assert verify_study(study)["study_verified"]
    assert study["runtime_invocation_evidence"]["invocation_exit_code"] == 0
    assert (out / "report.html").is_file()
    assert (out / "manifest.json").is_file()
    for mutate in ("approval", "receipt", "public_reference", "display", "verdict"):
        bad = deepcopy(study)
        if mutate == "approval":
            bad["authority"]["simulation_opt_in"] = False
        elif mutate == "receipt":
            bad["runtime_invocation_evidence"]["invocation_stdout_preimage"] = "{}"
        elif mutate == "public_reference":
            bad["public_reference"]["observations"][3]["altitude_m"] = 9999
        elif mutate == "display":
            bad["observations"][3]["altitude_m"] = 9999
        else:
            bad["runs"][0]["verification"]["evidence_verified"] = False
        bad["content_sha256"] = study_content_hash(bad)
        assert not verify_study(bad)["study_verified"], mutate


def test_cli_refuses_worker_without_opt_in(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_starship_3d.py"),
            "--worker-request",
            str(tmp_path / "does-not-exist"),
            "--worker-output",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "simulation_not_started"
    assert not (tmp_path / "out.json").exists()

#!/usr/bin/env python3
"""Opt-in, three-case booster tool regression from one saved M3 separation.

This is an offline physical-tool sensitivity probe, not another AI mission or
an extension of flight authority. All cases, limits, hashes and the backend are
frozen before any simulation. An honestly recorded impact remains a result.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
import math
import multiprocessing
import shutil
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = (("nominal", 0.0), ("minus_1000kg", -1000.0), ("plus_1000kg", 1000.0))
MAX_CASE_WALL_S = 600.0
MAX_OUTPUT_BYTES = 200_000_000
MAX_INPUT_BYTES = 200_000_000
MIN_FREE_BYTES = 5 * 1024**3
PROFILE = "examples/spaceflight/starship-sixdof-profile.json"
SITES = "examples/spaceflight/starship-return-sites-model-test.json"
CATCH = "examples/spaceflight/starship-catch-profile.json"
GOAL = "examples/spaceflight/starship-splashdown-goal.json"
SCRIPT = "scripts/probe_starship_booster_sensitivity.py"
PHYSICS_SOURCES = (
    "src/runtime/starship_sixdof_booster.py", "src/runtime/starship_sixdof_mission.py",
    "src/runtime/starship_sixdof.py", "src/runtime/starship_physics.py",
    "src/runtime/starship_sixdof_contact.py", PROFILE,
)
PHYSICAL_FIELDS = (
    "samples", "initial_state", "final_state", "final_vehicle", "contact",
    "guidance_configuration", "guidance_policy", "active_return_site",
    "return_site_sha256", "capture_inhibited", "divert_destination_reached",
    "outcome", "splashdown",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def write_new(path, value):
    """Write once, then return exactly the JSON representation verifiers read."""
    raw = canonical(value) + b"\n"
    with Path(path).open("xb") as stream:
        stream.write(raw)
    return json.loads(raw)


def backend():
    import numpy
    import scipy
    return {"python": sys.version.split()[0], "numpy": numpy.__version__, "scipy": scipy.__version__}


def source_hashes():
    paths = {str(p.relative_to(ROOT)) for p in (ROOT/"src/runtime").glob("starship_*.py")}
    paths.update((PROFILE, SITES, CATCH, GOAL, SCRIPT))
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(paths)}


def configuration(profile):
    from src.runtime.starship_return_sites import ReturnSites
    from src.runtime.starship_splashdown import SplashdownGoal
    sites = ReturnSites.from_dict(json.loads((ROOT/SITES).read_text()), profile=profile,
        catch_config=json.loads((ROOT/CATCH).read_text()))
    goal = SplashdownGoal.from_dict(json.loads((ROOT/GOAL).read_text()))
    goal.validate_site(sites.divert)
    return sites, goal


def verify_record(run, profile, goal, expected_separation):
    """Reuse independent saved-kinematics checks, not producer success flags."""
    from src.runtime import starship_sixdof_verifier as verifier
    from src.runtime.starship_splashdown_verifier import verify_splashdown
    issues = []
    try:
        verifier._json_tree(run)
        first, last, _, _, _ = verifier._run_basics(run, "$.booster", [0], profile, "booster")
        verifier._booster_envelope(run, profile, last, "$.booster")
        if run["booster_separation_state"] != expected_separation:
            raise ValueError("separation_input_changed")
        for key, value in expected_separation.items():
            if first.get(key) != value:
                raise ValueError("initial_sample_does_not_preserve_"+key)
        if any(run["outcome"].get(k) is not False for k in
               ("catch_verified", "landing_hardware_validated", "physical_execution")):
            raise ValueError("unsupported_physical_or_catch_claim")
    except verifier._Invalid as exc:
        issues.append(exc.issue)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        issues.append({"code": str(exc)})
    water = verify_splashdown(run, goal)
    return {"passed": not issues and water["passed"], "issues": issues + water["issues"],
        "water_entry": water, "independent_physics_validation": False,
        "scope": "saved states, contact kinematics, exact input and eight water-entry checks"}


def physical_digest(run):
    # The offline continuation has no managed-command event or shared-clock
    # receipt. All actual samples (including actuator commands), final states,
    # geometry and outcomes must nevertheless reproduce bit for bit.
    return digest({key: run[key] for key in PHYSICAL_FIELDS})


def perturbed_state(origin, delta):
    if type(delta) not in (int, float) or not math.isfinite(delta) or delta not in dict(CASES).values():
        raise ValueError("fuel_delta_not_registered")
    result = deepcopy(origin)
    result["propellant_kg"] += delta
    if result["propellant_kg"] <= 0:
        raise ValueError("nonpositive_perturbed_fuel")
    return result


def read_origin(path, expected_sha):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("bounded_regular_prefix_study_required")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected_sha:
        raise ValueError("input_study_sha256_mismatch")
    study = json.loads(raw)
    if study.get("schema") != "missionos.starship_m3_prefix_study.v1":
        raise ValueError("exact_m3_prefix_study_required")
    profile = study["profile"]
    if profile != json.loads((ROOT/PROFILE).read_text()):
        raise ValueError("profile_differs_from_current_checkout")
    current = source_hashes()
    if any(study["source_sha256"].get(name) != current[name] for name in PHYSICS_SOURCES):
        raise ValueError("recorded_physics_differs_from_current_checkout")
    sites, goal = configuration(profile)
    run = study["prefix_run"]["booster_run"]
    separated = study["prefix_run"]["booster_separation_state"]
    if (run["guidance_policy"] != "fixed_v1" or run["active_return_site"] != sites.divert.to_dict()
            or study["authority"]["booster_coast_control"] != "fixed_v1"
            or run["splashdown"]["goal"] != goal.to_dict()):
        raise ValueError("baseline_booster_configuration_mismatch")
    verification = verify_record(run, profile, goal.to_dict(), separated)
    if not verification["passed"] or not verification["water_entry"]["controlled_water_entry_envelope_met"]:
        raise ValueError("baseline_record_or_water_entry_not_verified")
    return {"schema": "missionos.starship_booster_probe_origin.v1", "profile": profile,
        "separation_state": separated, "separation_sha256": digest(separated),
        "baseline_physical_sha256": physical_digest(run), "baseline_verification": verification,
        "baseline_run_sha256": digest(run), "source_run_id": study["run_id"],
        "goal": goal.to_dict(), "input_study_sha256": expected_sha}


def diagnostics(run, profile):
    count = profile["booster"]["engine_count"]
    thrust = lambda row: sum(row.get("main_engine_thrust_n", []))
    rows = run["samples"]
    phases = {}
    for name in dict.fromkeys(row["phase"] for row in rows):
        own = [row for row in rows if row["phase"] == name]
        controlled = [row for row in own if "attitude_error_deg" in row.get("controller", {})]
        inertia = [max(sum(abs(x) for x in axis) for axis in row["inertia_kg_m2"]) for row in controlled]
        capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
        phases[name] = {"sample_count": len(own), "controlled_sample_count": len(controlled),
            "maximum_attitude_error_deg": max((row["controller"]["attitude_error_deg"] for row in controlled), default=None),
            "samples_above_90deg": sum(row["controller"]["attitude_error_deg"] > 90 for row in controlled),
            "samples_with_rcs_saturation": sum(any(e["throttle"] >= .99 for e in row["engine_states"][count:]) for row in controlled),
            "maximum_body_rate_rad_s": max(math.sqrt(sum(x*x for x in row["omega_body_rad_s"])) for row in own),
            "minimum_approximate_jet_alpha_capacity_indicator_rad_s2": min((capacity/x for x in inertia), default=None),
            "alpha_indicator_basis": "configured jet-pair torque divided by maximum absolute inertia row sum; not exact achievable axis acceleration",
            "controller_alpha_limit_rad_s2": profile["guidance"]["max_angular_acceleration_rad_s2"],
            "maximum_requested_torque_nm": max((max(abs(x) for x in row["controller"]["requested_torque_body_nm"]) for row in controlled), default=None),
            "maximum_aero_torque_nm": max((max(abs(x) for x in row["aero_torque_body_nm"]) for row in controlled), default=None),
            "sampled_main_thrust_max_n": max(thrust(row) for row in own),
            "fuel_start_kg": own[0]["propellant_kg"], "fuel_end_kg": own[-1]["propellant_kg"]}
    def snapshot(row):
        if row is None:
            return None
        return {**{key: row[key] for key in ("time_s", "altitude_m", "propellant_kg")},
            "main_thrust_n": thrust(row), "attitude_error_deg": row["controller"].get("attitude_error_deg"),
            "alignment_cosine": row["controller"].get("terminal_thrust_alignment_cosine")}
    terminal = [row for row in rows if row["phase"] == "booster_landing_burn"]
    return {"phases": phases,
        "terminal_request": next((e for e in run["events"] if e["event"] == "booster_landing_burn_requested"), None),
        "first_sampled_terminal_alignment": snapshot(next((r for r in terminal if r.get("controller", {}).get("terminal_braking_alignment_gate_met") is True), None)),
        "first_sampled_terminal_main_thrust_above_1kn": snapshot(next((r for r in terminal if thrust(r) > 1000), None)),
        "sample_interval_s": 2.0, "diagnostic_counts_are_samples_not_continuous_durations": True}


def check_frozen(output, protocol_sha, origin_sha):
    protocol = json.loads((output/"protocol.json").read_bytes())
    origin = json.loads((output/"origin.json").read_bytes())
    if digest(protocol) != protocol_sha or digest(origin) != origin_sha:
        raise ValueError("frozen_inputs_changed")
    if protocol["source_sha256"] != source_hashes() or protocol["backend"] != backend():
        raise ValueError("source_or_backend_changed")
    if protocol["cases"] != [{"case_id": name, "delta_fuel_kg": delta} for name, delta in CASES]:
        raise ValueError("case_set_changed")
    return protocol, origin


def case_worker(output_text, case_id, protocol_sha, origin_sha):
    """Spawned child: no Gateway, providers, credentials or command authority."""
    output = Path(output_text)
    destination = output/case_id
    started = time.monotonic()
    try:
        protocol, origin = check_frozen(output, protocol_sha, origin_sha)
        from src.runtime.starship_sixdof_booster import simulate_booster
        delta = dict(CASES)[case_id]
        state = perturbed_state(origin["separation_state"], delta)
        sites, goal = configuration(origin["profile"])
        run = simulate_booster(origin["profile"], state, guidance_policy="fixed_v1",
            return_sites=sites, splashdown_goal=goal, development_coast_control="fixed_v1")
        run = write_new(destination/"run.json", run)
        record = verify_record(run, origin["profile"], goal.to_dict(), state)
        same = physical_digest(run) == origin["baseline_physical_sha256"] if delta == 0 else None
        check_frozen(output, protocol_sha, origin_sha)
        contact = run.get("contact")
        summary = {"schema": "missionos.starship_booster_sensitivity_case.v1",
            "case_id": case_id, "delta_fuel_kg": delta, "protocol_sha256": protocol_sha,
            "origin_sha256": origin_sha, "initial_state_sha256": digest(state),
            "run_sha256": sha256((destination/"run.json").read_bytes()).hexdigest(),
            "record_verification": record, "nominal_physics_exactly_reproduced": same,
            "controlled_water_entry_envelope_met": record["water_entry"]["controlled_water_entry_envelope_met"],
            "failed_entry_checks": [k for k, v in run["splashdown"].get("checks", {}).items() if not v],
            "termination": run["outcome"]["termination"],
            "contact_speed_mps": contact["surface_relative_speed_mps"] if contact else None,
            "remaining_propellant_kg": run["final_state"]["propellant_kg"],
            "final_tilt_deg": run["outcome"]["final_tilt_deg"],
            "return_site_distance_m": run["outcome"]["return_site_distance_m"],
            "sample_count": len(run["samples"]), "integration_steps": run["outcome"]["integration_steps"],
            "diagnostics": diagnostics(run, origin["profile"]), "wall_time_s": time.monotonic()-started,
            "provider_calls": 0, "physical_execution": False, "operational_policy_admitted": False}
        write_new(destination/"summary.json", summary)
        if not record["passed"]:
            raise ValueError("saved_record_verification_failed")
        if same is False:
            raise ValueError("nominal_physics_did_not_reproduce")
    except Exception as exc:
        write_new(destination/"failure.json", {"error_type": type(exc).__name__, "error": str(exc),
            "case_id": case_id, "wall_time_s": time.monotonic()-started, "retry_performed": False})
        raise


def directory_bytes(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def run_bounded(output, case_id, protocol_sha, origin_sha):
    if shutil.disk_usage(output).free < MIN_FREE_BYTES:
        raise ValueError("minimum_free_space_before_case")
    (output/case_id).mkdir()
    child = multiprocessing.get_context("spawn").Process(target=case_worker,
        args=(str(output), case_id, protocol_sha, origin_sha))
    start = time.monotonic()
    child.start()
    reason = None
    while child.is_alive():
        if time.monotonic()-start >= MAX_CASE_WALL_S:
            reason = "case_wall_time_cap"
        elif directory_bytes(output) >= MAX_OUTPUT_BYTES:
            reason = "campaign_output_cap"
        if reason:
            child.terminate()
            child.join(timeout=3)
            if child.is_alive():
                child.kill()
            break
        child.join(timeout=.2)
    child.join()
    if directory_bytes(output) >= MAX_OUTPUT_BYTES:
        reason = reason or "campaign_output_cap"
    result = {"case_id": case_id, "exit_code": child.exitcode,
        "interruption": reason, "wall_time_s": time.monotonic()-start,
        "output_bytes": directory_bytes(output), "worker_pid": child.pid, "retry_performed": False}
    write_new(output/case_id/"attempt.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True, help="Exact saved M3 concurrent-prefix study")
    parser.add_argument("--study-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approve-simulation", action="store_true")
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("--approve-simulation is required")
    if len(args.study_sha256) != 64 or any(c not in "0123456789abcdef" for c in args.study_sha256):
        parser.error("expected lowercase SHA256 required")
    output = args.output_dir.resolve()
    if output.exists():
        parser.error("output must be a new directory; never overwrite a trial")
    try:
        origin = read_origin(args.study, args.study_sha256)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    frozen = source_hashes()
    protocol = {"schema": "missionos.starship_booster_sensitivity_protocol.v1",
        "cases": [{"case_id": name, "delta_fuel_kg": delta} for name, delta in CASES],
        "source_sha256": frozen, "backend": backend(), "input_study_sha256": args.study_sha256,
        "origin_sha256": digest(origin), "goal": origin["goal"], "attempts_per_case": 1,
        "maximum_case_wall_s": MAX_CASE_WALL_S, "maximum_total_output_bytes": MAX_OUTPUT_BYTES,
        "minimum_free_bytes_before_each_case": MIN_FREE_BYTES,
        "maximum_simulated_duration_s": origin["profile"]["booster_return"]["max_duration_s"],
        "nominal_gate": "exact_physical_samples_and_outcomes_before_any_perturbation",
        "physical_failure_policy": "preserve_result_and_continue_registered_cases",
        "execution_failure_policy": "stop_without_retry",
        "scope": "saved-separation fuel sensitivity of current fixed_v1 splashdown tool",
        "launch_perturbation_modeled": False, "model_calls": 0,
        "ai_comparison": False, "operational_policy_admitted": False, "physical_execution": False}
    output.mkdir(parents=True, mode=0o700)
    write_new(output/"origin.json", origin)
    write_new(output/"protocol.json", protocol)
    protocol_sha, origin_sha = digest(protocol), digest(origin)
    (output/"protocol.sha256").write_text(protocol_sha+"\n")
    attempts, summaries, error = [], [], None
    try:
        for case_id, _ in CASES:
            check_frozen(output, protocol_sha, origin_sha)
            attempt = run_bounded(output, case_id, protocol_sha, origin_sha)
            attempts.append(attempt)
            summary_path = output/case_id/"summary.json"
            summary = json.loads(summary_path.read_bytes()) if summary_path.exists() else None
            if summary is not None:
                summaries.append(summary)
            if attempt["exit_code"] != 0 or attempt["interruption"]:
                raise ValueError("case_execution_failed:"+case_id)
            if summary is None or not summary["record_verification"]["passed"]:
                raise ValueError("case_record_invalid:"+case_id)
            if case_id == "nominal" and summary["nominal_physics_exactly_reproduced"] is not True:
                raise ValueError("nominal_physics_did_not_reproduce")
        check_frozen(output, protocol_sha, origin_sha)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    result = {"schema": "missionos.starship_booster_sensitivity_study.v1",
        "protocol_sha256": protocol_sha, "source_sha256": frozen, "backend": protocol["backend"],
        "cases": summaries, "attempts": attempts, "execution_error": error,
        "all_cases_executed_and_record_verified": error is None and len(summaries) == len(CASES),
        "all_three_entry_envelopes_met": error is None and len(summaries) == len(CASES)
            and all(row["controlled_water_entry_envelope_met"] for row in summaries),
        "perturbation_qualification_claimed": False, "retry_performed": False,
        "provider_calls": 0, "ai_value_assessed": False, "physical_execution": False}
    write_new(output/"summary.json", result)
    print(json.dumps({key: result[key] for key in ("all_cases_executed_and_record_verified", "all_three_entry_envelopes_met", "execution_error")}))
    return 0 if error is None else 2


if __name__ == "__main__":
    raise SystemExit(main())

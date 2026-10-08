"""Opt-in late engine-loss probe: preserve terminal violations and final outcomes."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_return_feasibility import readiness, sources, POLICY  # noqa: E402
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402
from src.runtime.starship_mission_director import contract  # noqa: E402
from src.runtime.starship_mission_director_verifier import _return_execution_checks  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("Explicit simulation opt-in and a fresh output directory are required")
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    _, _, reason = readiness(profile)
    if reason:
        parser.error(reason)
    args.output_dir.mkdir(parents=True)
    before = sources()
    run = simulate(profile, scenario="terminal_engine_out", return_policy=POLICY)
    study = json.loads(json.dumps({"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
        "runs": [run], "provenance": {"source_sha256": before, "physical_execution_invoked": False,
                                      "starship_vehicle_validated": False}}, allow_nan=False))
    (args.output_dir/"study.json").write_text(json.dumps(study, allow_nan=False, separators=(",", ":")))
    run = study["runs"][0]
    record = verify_study(study, expected_scenario="terminal_engine_out")
    policy = verify_retained_return(run, profile, expected_policy=POLICY)
    margin_error = None
    try:
        _return_execution_checks(run, profile, contract("fixture"))
    except (ValueError, KeyError, TypeError) as error:
        margin_error = str(error)
    preserved = (run["outcome"]["terminal_qualification_violated"] is True
                 and run["retained_return"]["terminal_feasibility"]["passed"] is False
                 and run["final_state"]["time_s"] > run["retained_return"]["trigger"]["time_s"]
                 and run["outcome"]["termination"] in ("surface_impact", "low_speed_surface_contact", "time_limit",
                                                      "angular_rate_envelope_exceeded", "numerical_failure"))
    result = {"source_unchanged": before == sources(), "record": record, "policy": policy,
              "margin_verification_error": margin_error, "violation_and_outcome_preserved": preserved,
              "outcome": run["outcome"], "recovery_success_claimed": False, "physical_execution": False}
    (args.output_dir/"result.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    print(json.dumps({"record_verified": record["passed"], "policy_verified": policy["passed"],
                      "margin_verification_error": margin_error, "violation_and_outcome_preserved": preserved}))
    return 0 if before == sources() and record["passed"] and policy["passed"] and not margin_error and preserved else 2


if __name__ == "__main__":
    raise SystemExit(main())

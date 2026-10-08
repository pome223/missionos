"""Opt-in launch-derived Ship return qualification with a frozen release limit.

The fixture releases real rigid payloads with conserved separation momentum.
No AI is called; a release limit is a local experiment, never a MissionOS grant.
Failed flights and qualification failures are retained without automatic retry.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402
from src.runtime import starship_sixdof as dyn, starship_physics as env  # noqa: E402

SOURCES=("scripts/run_starship_return_qualification.py","src/runtime/starship_sixdof_mission.py", "src/runtime/starship_ship_return.py",
         "src/runtime/starship_sixdof.py","src/runtime/starship_sixdof_separation.py",
         "src/runtime/starship_sixdof_contact.py","src/runtime/starship_fin_allocation.py",
         "src/runtime/starship_entry_trim.py",
         "src/runtime/starship_return_feasibility.py", "src/runtime/starship_return_feasibility_verifier.py",
         "src/runtime/starship_physics.py","src/runtime/starship_retained_return.py",
         "src/runtime/starship_attitude_reference.py","src/runtime/starship_sixdof_booster.py",
         "src/runtime/starship_wind.py","src/runtime/starship_sixdof_verifier.py",
         "src/runtime/starship_retained_return_verifier.py","examples/spaceflight/starship-sixdof-profile.json")


def sources():
    return {name:sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def write(path,value):
    path.write_text(json.dumps(value,indent=2,allow_nan=False)+"\n")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation",action="store_true")
    parser.add_argument("--retained",type=int,required=True)
    parser.add_argument("--legacy-allocation",action="store_true")
    parser.add_argument("--wind-trim",action="store_true",help="Experimental shared wind-bank/trim/state-terminal policy, including zero retained")
    parser.add_argument("--initial-fuel-offset-kg",type=float,default=0.,help="Explicit launch initial-condition perturbation in [-1000,1000] kg")
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args(argv)
    if not args.approve_simulation or not 0 <= args.retained <= 26:
        parser.error("explicit opt-in and retained count in [0,26] are required")
    if args.output_dir.exists():
        parser.error("use a fresh output directory; failed outcomes cannot be overwritten")
    profile=json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    if not math.isfinite(args.initial_fuel_offset_kg) or abs(args.initial_fuel_offset_kg)>1000.:
        parser.error("initial-fuel perturbation must be finite and within +/-1000 kg")
    profile["ship"]["propellant_kg"] += args.initial_fuel_offset_kg
    if args.wind_trim and args.legacy_allocation:
        parser.error("wind-trim requires bounded allocation")
    policy_id = "trimmed_state_terminal_v4" if args.wind_trim else "mass_state_terminal_v3"
    scope={"release_limit":profile["payload"]["count"]-args.retained,"bounded_ship_flaps":not args.legacy_allocation,
           "application":"wind_trim_state_return_v2" if args.wind_trim else "retained_policy_active_only_v1"}
    before=sources()
    args.output_dir.mkdir(parents=True)
    write(args.output_dir/"inputs.json",{"source_sha256":before,"scope":scope,"profile":profile,
        "allocation_application":"active state return including zero retained" if args.wind_trim else "only while retained-return policy is active or triggered; zero-retained uses unchanged nominal allocation",
        "qualification_limits":{"contact_speed_mps":5.,"tilt_deg":5.,"body_rate_rad_s":.02,
                                "fuel_reserve_kg":profile["ship"]["return_reserve_kg"]},
        "missionos_approval":None,"model_inference_invoked":False,"physical_execution":False})
    for name in before:
        dest=args.output_dir/"source"/name
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    try:
        run=json.loads(json.dumps(simulate(profile,return_policy=policy_id,
            _development_return_qualification=scope),allow_nan=False))
        run["development_source_sha256"]=before
        study={"schema":"missionos.starship_sixdof_study.v1","profile":profile,"runs":[run],
               "provenance":{"physical_execution_invoked":False,"starship_vehicle_validated":False,
                             "source_sha256":before,"hashes_are_execution_attestation":False}}
        write(args.output_dir/"study.json",study)
        record=verify_study(study,expected_scenario="launch",expected_development_return_qualification=scope)
        policy=verify_retained_return(run,profile,expected_policy=policy_id,
            expected_development_return_qualification=scope)
        write(args.output_dir/"verification.json",{"record":record,"return_policy":policy})
        outcome=run["outcome"]
        contact=outcome["contact_receipt"]
        tilt = (math.degrees(math.acos(max(-1.,min(1.,env.dot(
            dyn.rotate(contact["q_body_to_eci"],(0.,0.,1.)),contact["surface_normal_eci"]))))) if contact else None)
        rate=env.norm(run["final_state"]["omega_body_rad_s"])
        source_unchanged=before==sources()
        qualified=bool(source_unchanged and record["passed"] and policy["passed"]
            and outcome["orbit_gate_reached"] and outcome["payload_released_count"]==scope["release_limit"]
            and contact is not None and contact["surface_relative_speed_mps"]<=5.
            and tilt<=5. and rate<=.02
            and contact["propellant_kg"]>=profile["ship"]["return_reserve_kg"])
        result={"retained_count":args.retained,"scope":scope,"termination":outcome["termination"],
            "initial_fuel_offset_kg":args.initial_fuel_offset_kg,
            "final_phase":outcome["phase"],"released_count":outcome["payload_released_count"],
            "contact_speed_mps":contact["surface_relative_speed_mps"] if contact else None,
            "remaining_fuel_kg":run["final_state"]["propellant_kg"],"policy_status":run["retained_return"]["status"],
            "contact_tilt_deg":tilt,"final_body_rate_rad_s":rate,
            "record_checks_passed":record["passed"],"policy_checks_passed":policy["passed"],
            "source_unchanged":source_unchanged,"provisional_speed_and_reserve_gate_met":qualified,
            "recovery_certified":False,"full_0_to_26_qualification_complete":False,
            "physical_execution":False,"model_inference_invoked":False,
            "study_sha256":sha256((args.output_dir/"study.json").read_bytes()).hexdigest()}
        write(args.output_dir/"result.json",result)
        print(json.dumps(result,allow_nan=False),flush=True)
        return 0 if source_unchanged and record["passed"] and policy["passed"] else 2
    except Exception as error:
        write(args.output_dir/"failure.json",{"exception":type(error).__name__,"detail":str(error),
             "source_sha256_after":sources()})
        raise


if __name__=="__main__":
    raise SystemExit(main())

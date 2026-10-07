"""Opt-in short Ship entry comparison from one recorded physical checkpoint.

Only surface allocation changes. No vehicle coefficient, jet authority, target
frame, ignition condition or actual state is adjusted. This is not a launch,
return qualification, MissionOS grant, or model inference experiment.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime import starship_physics as env, starship_sixdof as dyn  # noqa: E402
from src.runtime.starship_attitude_reference import ConditionedGeographicFrame  # noqa: E402
from src.runtime.starship_fin_allocation import MOMENT_PRIORITY_POLICY_ID  # noqa: E402
from src.runtime.starship_sixdof_mission import _attitude, _sample, control, point_state, vehicle  # noqa: E402

SOURCES = ("scripts/run_starship_entry_allocation_comparison.py", "src/runtime/starship_fin_allocation.py",
           "src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof.py",
           "src/runtime/starship_physics.py", "src/runtime/starship_attitude_reference.py",
           "src/runtime/starship_wind.py", "examples/spaceflight/starship-sixdof-profile.json")


def sources():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+"\n")


def inputs(run, profile):
    if (run.get("retained_return", {}).get("policy_id") != "mass_state_terminal_v3"
            or not 0 <= len(run.get("satellites", [])) <= profile["payload"]["count"]):
        raise ValueError("requires_recorded_conditioned_ship_return")
    rows = [s for s in run["samples"] if s["phase"] == "ballistic_return" and "controller" in s]
    first = next(i for i, s in enumerate(rows) if s["dynamic_pressure_pa"] > 50.)
    eligible = [s for s in rows[:first] if s["dynamic_pressure_pa"] <= 40.]
    if not eligible:
        raise ValueError("missing_pre_gate_checkpoint")
    seed = eligible[-1]
    if seed["controller"].get("attitude_reference", {}).get("mode") != "legacy_geographic":
        raise ValueError("checkpoint_reference_state_not_restorable")
    state = dyn.state_from_dict({key: seed[key] for key in ("time_s", "r_eci_m", "v_eci_mps",
        "q_body_to_eci", "omega_body_rad_s", "propellant_kg", "engine_states", "flap_angles_rad")})
    body = vehicle(profile, payload_count=profile["payload"]["count"]-len(run["satellites"]))
    observed = dyn.observe(state, body)
    for key in ("mass_kg", "inertia_kg_m2", "com_z_m"):
        actual = observed["com_body_m"][2] if key == "com_z_m" else observed[key]
        if not np.allclose(actual, seed[key], rtol=1e-12, atol=1e-6):
            raise ValueError("checkpoint_vehicle_properties_mismatch")
    if len(state.engine_states) != len(body.engines) or len(state.flap_angles_rad) != len(body.aero_panels):
        raise ValueError("checkpoint_actuator_identity_mismatch")
    return state, body, seed


def continue_entry(initial, body, seed, profile, duration, *, candidate, checkpoint=None):
    state = initial
    g = profile["guidance"]
    reference = ConditionedGeographicFrame(seed["controller"]["target_q_body_to_eci"], state.time_s,
        maximum_roll_rate_rad_s=g["max_angular_acceleration_rad_s2"]/g["attitude_frequency_rad_s"])
    end = state.time_s+duration
    samples, metrics = [], []
    maximum_steps = math.ceil(duration/profile["integration"]["powered_dt_s"])+1
    for _ in range(maximum_steps):
        if state.time_s >= end-1e-9:
            break
        point = point_state(state)
        up, _, north = env.local_frame(point)
        flow = env.unit(env.air_relative_velocity(point))
        lift = env.add(up, env.scale(flow, -env.dot(up, flow)))
        lift = env.unit(lift) if env.norm(lift) > 1e-8 else north
        axis = env.add(env.scale(flow, math.cos(math.radians(g["entry_alpha_deg"]))),
                       env.scale(lift, math.sin(math.radians(g["entry_alpha_deg"]))))
        preferred = _attitude(axis, env.scale(north, -1))
        target = reference.target(axis, env.scale(north, -1), preferred, time_s=state.time_s)
        dt = min(profile["integration"]["powered_dt_s"], end-state.time_s)
        command, diagnostic = control(state, body, target, 0., 0, profile, use_flaps=True,
            development_fin_allocation=candidate, control_interval_s=dt,
            development_fin_policy=MOMENT_PRIORITY_POLICY_ID if candidate else "finite_regularized_fins_v1")
        diagnostic["attitude_reference"] = reference.diagnostics
        observed = dyn.observe(state, body)
        sample = _sample(state, body, "ballistic_return", command, diagnostic)
        pressure = observed["dynamic_pressure_pa"]
        metrics.append({"time_s":state.time_s,"dynamic_pressure_pa":pressure,
            "attitude_error_deg":diagnostic["attitude_error_deg"],
            "body_rate_rad_s":env.norm(state.omega_body_rad_s),
            "aero_torque_body_nm":list(observed["aero_torque_body_nm"]),
            "requested_residual_torque_body_nm":diagnostic["requested_torque_body_nm"],
            "rcs_capacity_nm":2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]})
        samples.append(sample)
        state = dyn.step(state, body, command, dt)
        if checkpoint is not None and len(metrics) % 200 == 0:
            checkpoint({"integration_steps":len(metrics),"latest_state":asdict(state),
                        "latest_metric":metrics[-1],"completed":False})
        if env.ecef_to_geodetic(state.r_eci_m)[2] < 50000 or env.norm(state.omega_body_rad_s) > 5.:
            raise ValueError("short_entry_domain_exited")
    else:
        raise ValueError("entry_step_limit_reached")
    window = [m for m in metrics if 50. <= m["dynamic_pressure_pa"] <= 600.]
    summary = {"integration_steps":len(metrics),"window_sample_count":len(window),
        "maximum_sampled_attitude_error_deg":max((m["attitude_error_deg"] for m in window),default=None),
        "maximum_sampled_body_rate_rad_s":max((m["body_rate_rad_s"] for m in window),default=None),
        "maximum_sampled_aero_torque_component_nm":max((max(abs(x) for x in m["aero_torque_body_nm"]) for m in window),default=None),
        "maximum_sampled_residual_over_jet_capacity":max((max(abs(x) for x in m["requested_residual_torque_body_nm"])/m["rcs_capacity_nm"] for m in window),default=None),
        "propellant_used_kg":initial.propellant_kg-state.propellant_kg,
        "final_time_s":state.time_s,"final_dynamic_pressure_pa":dyn.observe(state,body)["dynamic_pressure_pa"],
        "sampled_metrics_are_continuous_extrema":False,"return_qualified":False}
    return {"method":"bounded_moment_priority" if candidate else "legacy_clipped_inverse",
        "initial_state":asdict(initial),"final_state":asdict(state),"samples":samples,
        "metrics":metrics,"summary":summary,"physical_execution":False,"model_inference_invoked":False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation",action="store_true")
    parser.add_argument("--run-record",type=Path,required=True)
    parser.add_argument("--profile",type=Path,default=ROOT/"examples/spaceflight/starship-sixdof-profile.json")
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--duration-s",type=float,default=180.)
    args = parser.parse_args(argv)
    if not args.approve_simulation or not math.isfinite(args.duration_s) or not 0 < args.duration_s <= 200.:
        parser.error("explicit opt-in and a duration in (0, 200] seconds are required")
    if args.output_dir.exists():
        parser.error("use a fresh output directory; preserve failed comparisons")
    before = sources()
    raw = args.run_record.read_bytes()
    run = json.loads(gzip.decompress(raw) if args.run_record.suffix == ".gz" else raw)
    if run.get("schema") == "missionos.starship_sixdof_study.v1":
        if type(run.get("runs")) is not list or len(run["runs"]) != 1:
            raise ValueError("requires_single_source_run")
        run = run["runs"][0]
    profile = json.loads(args.profile.read_text())
    initial, body, seed = inputs(run, profile)
    args.output_dir.mkdir(parents=True)
    write(args.output_dir/"inputs.json",{"record_sha256":sha256(raw).hexdigest(),"profile":profile,
        "initial_state":asdict(initial),"retained_count":profile["payload"]["count"]-len(run["satellites"]),
        "source_sha256":before,"duration_s":args.duration_s,"candidate_policy":MOMENT_PRIORITY_POLICY_ID,
        "criteria":{"pressure_window_pa":[50.,600.],"candidate_maximum_attitude_error_deg":5.},
        "scope":"local_development_checkpoint_continuation","production_policy_admitted":False})
    for name in before:
        destination=args.output_dir/"source"/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((ROOT/name).read_bytes())
    results=[]
    try:
        for candidate in (False,True):
            method="bounded_moment_priority" if candidate else "legacy_clipped_inverse"
            def checkpoint(record):
                write(args.output_dir/(method+"-progress.json"),record)
                print(json.dumps({"method":method,"steps":record["integration_steps"],
                    "time_s":record["latest_state"]["time_s"],
                    "pressure_pa":record["latest_metric"]["dynamic_pressure_pa"],
                    "attitude_error_deg":record["latest_metric"]["attitude_error_deg"]}),flush=True)
            result=continue_entry(initial,body,seed,profile,args.duration_s,candidate=candidate,
                                  checkpoint=checkpoint)
            filename=result["method"]+".json"
            write(args.output_dir/filename,result)
            item={"method":result["method"],"file":filename,
                "sha256":sha256((args.output_dir/filename).read_bytes()).hexdigest(),**result["summary"]}
            results.append(item)
            print(json.dumps(item,allow_nan=False),flush=True)
        if before != sources():
            raise ValueError("sources_changed_during_execution")
        write(args.output_dir/"comparison.json",{"source_unchanged":True,"results":results,
            "initial_states_equal":True,"only_allocation_policy_changed":True,
            "new_full_flight_executed":False,"return_qualified":False,"missionos_approval":None})
    except Exception as error:
        write(args.output_dir/"failure.json",{"exception":type(error).__name__,"detail":str(error),
            "completed_methods":results,"source_sha256_after":sources()})
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

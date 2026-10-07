"""Opt-in five scheduled-cutoff trials from one saved physical separation.

No vehicle coefficient, initial fuel, arrival gate or downstream controller is
tuned. This is offline guidance development, outside MissionOS dispatch scope.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_booster_recovery import simulate_recovery  # noqa: E402
from src.runtime.starship_booster_recovery_verifier import verify_recovery, _arrival  # noqa: E402
from src.runtime.starship_booster_catch import simulate_catch  # noqa: E402
from src.runtime.starship_sixdof_catalog import SIXDOF_SOURCES, SIXDOF_PROFILE, CATCH_PROFILE  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402

OFFSETS_S = (0., 5., 10., 15., 20.)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, allow_nan=False, separators=(",", ":"))
        stream.write("\n")


def sources():
    names = (*SIXDOF_SOURCES, "src/runtime/starship_mission_control.py", "scripts/run_starship_boostback_comparison.py")
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def inputs(study):
    if study.get("profile") != json.loads((ROOT / SIXDOF_PROFILE).read_text()) or study.get("catch_profile") != json.loads((ROOT / CATCH_PROFILE).read_text()):
        raise ValueError("reference_configuration_differs_from_checkout")
    verdict = verify_study(study, expected_scenario="launch")
    if verdict.get("passed") is not True or len(study["runs"]) != 1:
        raise ValueError("reference_study_verification_failed")
    booster = study["runs"][0].get("booster_run")
    if not isinstance(booster, dict):
        raise ValueError("reference_has_no_separation")
    cutoffs = [e for e in booster["events"] if e["event"] == "boostback_complete_rate_settle"]
    ignitions = [e for e in booster["events"] if e["event"] == "boostback_ignition"]
    if len(cutoffs) != 1 or len(ignitions) != 1 or cutoffs[0]["time_s"]-20. <= ignitions[0]["time_s"]:
        raise ValueError("five_cutoffs_must_follow_reference_ignition")
    return booster["booster_separation_state"], cutoffs[0]["time_s"], booster, verdict


def boundary_snapshot(event, profile, config):
    if event is None:
        return None
    state = event["state"]
    observation = _arrival(state, profile, config)
    return {"time_s": state["time_s"], "state": state, "tower_observation": observation}


def summarize(run, catch_run, verification, profile, config, cutoff_time, elapsed):
    events = run["events"]
    cutoff = next((e for e in events if e["event"] == "boostback_complete_rate_settle"), None)
    settled = next((e for e in events if e["event"] == "powered_rate_settled_cutoff"), None)
    landing = next((e for e in events if e["event"] == "landing_stage_requested"), None)
    scheduled = bool(cutoff and cutoff.get("cutoff_basis") == "development_scheduled_cutoff")
    # The prefix includes actual engines/fins and the integrated attitude, not
    # just plotted position. Event states give exact landing-request boundaries.
    fins = [abs(a) for s in run["samples"] for a in s["flap_angles_rad"][3:]]
    rcs = [e["throttle"] for s in run["samples"] for e in s["engine_states"][profile["booster"]["engine_count"]:]]
    return {"cutoff_time_s": cutoff_time, "scheduled_cutoff_executed": scheduled,
            "cutoff": boundary_snapshot(cutoff, profile, config),
            "coast_start": boundary_snapshot(settled, profile, config),
            "landing_request": boundary_snapshot(landing, profile, config),
            "outcome": run["outcome"], "final_propellant_kg": run["final_state"]["propellant_kg"],
            "arrival": run["recovery_record"]["handoff"]["observation"],
            "max_sampled_fin_deflection_deg": math.degrees(max(fins, default=0.)),
            "max_sampled_rcs_throttle": max(rcs, default=0.),
            "catch_invoked": catch_run is not None,
            "catch_outcome": catch_run["outcome"] if catch_run else None,
            "verification": verification, "wall_time_s": elapsed,
            "candidate_supported": bool(scheduled and verification["passed"] and verification["handoff_reached"]
                                         and verification["catch_supported_after_handoff"]),
            "production_policy_admitted": False, "physical_execution": False}


def physical_difference(first, second):
    # Quaternion components are compared directly because the deterministic
    # replay should retain the same representation as the reference.
    result = {key: max(abs(a-b) for a, b in zip(first[key], second[key]))
              for key in ("r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "flap_angles_rad")}
    result.update({key: abs(first[key]-second[key]) for key in ("time_s", "propellant_kg")})
    result["engine_states_equal"] = first["engine_states"] == second["engine_states"]
    result["within_tolerance"] = (result["engine_states_equal"] and result["r_eci_m"] <= 1e-5
        and result["v_eci_mps"] <= 1e-7 and result["time_s"] <= 1e-7 and result["propellant_kg"] <= 1e-5
        and all(result[k] <= 1e-8 for k in ("q_body_to_eci", "omega_body_rad_s", "flap_angles_rad")))
    return result


def report(bundle):
    rows = []
    for item in bundle["conditions"]:
        outcome = item["outcome"]
        landing = item["landing_request"]
        fuel = landing["state"]["propellant_kg"] if landing else None
        cells = [f'{item["cutoff_time_s"]:.1f}', f'{fuel/1000:.2f}' if fuel is not None else "—",
                 f'{item["final_propellant_kg"]/1000:.2f}', f'{outcome["return_site_distance_m"]:,.1f}',
                 f'{outcome["final_ground_speed_mps"]:.2f}', f'{outcome["final_tilt_deg"]:.2f}',
                 "到達" if item["verification"]["handoff_reached"] else "未達",
                 "支持成立" if item["candidate_supported"] else "未成立",
                 "整合" if item["verification"]["passed"] else "検証失敗"]
        rows.append("<tr>"+"".join("<td>"+escape(c)+"</td>" for c in cells)+"</tr>")
    summary = "この5条件ではキャッチ未成立。" if not any(c["candidate_supported"] for c in bundle["conditions"]) else "支持が成立した候補は、本番経路での再検証が必要です。"
    return '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Starship boostback比較</title><style>body{background:#071019;color:#e5edf5;font:16px system-ui;max-width:1150px;margin:40px auto;padding:20px}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #344454;padding:12px;text-align:right}p{line-height:1.8}a{color:#8ecbff}</style><h1>6DOF boostback停止時刻の比較</h1><p>同じ保存分離状態から、位置・速度・姿勢・角速度、各エンジン・フィンの有限応答を積分。質量・空力・初期燃料・接触条件と、停止後の制御は固定。</p><p>'+summary+'</p><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in ("停止 T+秒", "着陸要求時燃料 t", "終端燃料 t", "タワー距離 m", "対地速度 m/s", "傾斜 °", "到達条件", "接触・支持", "記録検証"))+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table><p>これは保存した分離状態からの開発実験です。発射からの再実行、SpaceX実機の妥当性、独立した運動方程式の再積分、MissionOS本番方針の採用を意味しません。検証は保存状態の整合性・到達条件・接触を独立に再計算します。</p><p><a href="comparison.json">比較記録 JSON</a></p></html>'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--separation-study", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.approve_simulation:
        parser.error("Explicit --approve-simulation is required")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output directory must be empty; retained failures cannot be overwritten")
    before = sources()
    reference_bytes = args.separation_study.read_bytes()
    study = json.loads(reference_bytes)
    initial, reference_time, reference_run, reference_verification = inputs(study)
    profile, config = study["profile"], study["catch_profile"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write(args.output_dir / "inputs.json", {"initial_state": initial, "profile": profile, "catch_profile": config,
        "reference_study_sha256": sha256(reference_bytes).hexdigest(), "reference_verification": reference_verification,
        "reference_cutoff_time_s": reference_time, "offsets_s": OFFSETS_S, "source_sha256": before})
    # Preserve exact inspected sources alongside receipts, without committing
    # private/generated evidence. All conditions use one unchanged process.
    for name in before:
        path = args.output_dir / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    conditions, runs = [], []
    try:
        for offset in OFFSETS_S:
            cutoff = reference_time-offset
            started = time.monotonic()
            run = simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config), _development_cutoff_time_s=cutoff)
            catch_run = None
            if run["recovery_record"]["handoff"]["eligible"]:
                catch_run = simulate_catch(profile, config, initial_state=run["final_state"], duration_s=30., control_policy="net_thrust_trim_v1")
            # Verify persisted JSON, not NumPy-containing producer objects.
            persisted = json.loads(json.dumps({"run": run, "catch_run": catch_run}, allow_nan=False))
            run, catch_run = persisted["run"], persisted["catch_run"]
            verification = verify_recovery(run, initial, profile, config, catch_run=catch_run, development_cutoff_time_s=cutoff)
            filename = f"cutoff-minus-{int(offset):02d}.json"
            write(args.output_dir / filename, persisted)
            item = summarize(run, catch_run, verification, profile, config, cutoff, time.monotonic()-started)
            item["run_file"] = filename
            item["run_sha256"] = sha256((args.output_dir / filename).read_bytes()).hexdigest()
            write(args.output_dir / f"cutoff-minus-{int(offset):02d}-summary.json", item)
            conditions.append(item)
            runs.append(run)
            print(json.dumps({"offset_s": offset, "termination": item["outcome"]["termination"],
                "speed_mps": item["outcome"]["final_ground_speed_mps"], "distance_m": item["outcome"]["return_site_distance_m"],
                "fuel_kg": item["final_propellant_kg"], "verified": verification["passed"]}, allow_nan=False), flush=True)
        earliest = reference_time-max(OFFSETS_S)
        prefixes = [[c["state"] for c in r["recovery_record"]["checkpoints"] if c["time_s"] < earliest-1e-9] for r in runs]
        prefix_equal = all(p == prefixes[0] for p in prefixes[1:])
        replay_difference = physical_difference(runs[0]["final_state"], reference_run["final_state"])
        after = sources()
        passed = (before == after and prefix_equal and replay_difference["within_tolerance"]
                  and all(c["verification"]["passed"] and c["scheduled_cutoff_executed"] for c in conditions))
        bundle = {"schema": "missionos.starship_boostback_comparison.v1", "verification_passed": passed,
            "conditions": conditions, "common_prefix_equal": prefix_equal, "common_prefix_checkpoint_count": len(prefixes[0]),
            "initial_state_sha256": digest(initial), "profile_sha256": before[SIXDOF_PROFILE], "catch_profile_sha256": before[CATCH_PROFILE],
            "source_sha256_before": before, "source_sha256_after": after, "source_unchanged": before == after,
            "fixed_cutoff_reference_replay": replay_difference, "production_policy_admitted": False,
            "full_launch_reexecuted": False, "physical_execution": False, "integrator_independently_reexecuted": False}
        write(args.output_dir / "comparison.json", bundle)
        (args.output_dir / "report.html").write_text(report(bundle))
        return 0 if passed else 2
    except Exception as exc:
        write(args.output_dir / "failure.json", {"exception": type(exc).__name__, "detail": str(exc), "completed_conditions": len(conditions), "source_sha256_after": sources()})
        raise


if __name__ == "__main__":
    raise SystemExit(main())

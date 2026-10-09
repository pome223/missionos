"""Opt-in coupled 6DOF development missions and offline attitude replay."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_report import build_report  # noqa: E402
from src.runtime.starship_flight_supervision import FlightSupervision  # noqa: E402
from src.runtime.starship_sixdof_catalog import FIXED_RETURN_POLICY, RETAINED_RETURN_POLICY, CONTINUOUS_RETURN_POLICY, CATCH_PROFILE, CATCH_SCENARIOS  # noqa: E402
from src.runtime.starship_sixdof_catalog import BOOSTER_RECOVERY_POLICY  # noqa: E402

COVERAGE = [
    {"component": "運動方程式", "implementation": "ECI位置・速度 + body→ECI quaternion + body角速度、非対角慣性、RK4", "status": "6DOFを連成積分"},
    {"component": "環境", "implementation": "WGS84楕円体、自転、中心重力+J2、中心場の重力傾斜トルク、US76下層大気、任意の合成ENU風", "status": "86 km超は指数近似。既定は無風。合成風は気象再現ではない。J2潮汐・天文時刻補正は未実装"},
    {"component": "推進・制御", "implementation": "33/6エンジンの配置、各ジンバル、有限の応答・速度制限、配置を持つRCS噴射対", "status": "配置・性能・PD誘導・RCS設計は仮定。燃焼室/配管は未実装"},
    {"component": "質量特性", "implementation": "燃料消費から総質量・重心・全慣性テンソルを毎RK段で更新", "status": "固定形状の密度低下モデル。噴流減衰/流出運動量/スロッシング未実装"},
    {"component": "空力・フラップ", "implementation": "機体軸別抗力、局所流速ω×r、4フラップ/3フィンの力・モーメントと有限作動", "status": "汎用平板近似。実機のMach/AoA空力表・grid fin格子流れ未実装"},
    {"component": "段分離", "implementation": "位置・速度・姿勢・角速度を継承する剛体分離と運動量検査", "status": "瞬時分離。ホットステージの噴流・接触は未実装"},
    {"component": "衛星放出", "implementation": "有限慣性の子機分離、等反作用の衝撃と回転、分離後の6DOF軌道伝播", "status": "配置は同一点に集約した質量モデル。装置内干渉・衛星運用は未実装"},
    {"component": "帰還・接地", "implementation": "分離後boosterも独立積分、機体円筒全体と楕円体の初回接触を探索。別の終端初期条件から有限アームと接触荷重を計算", "status": "仮定した帰還誘導と支持機構。打ち上げからのキャッチ成功、水面・構造・実機機構は未検証"},
    {"component": "熱・構造", "implementation": "この版は熱流束・TPS・弾性モード・破壊を計算しない", "status": "未実装。大気突入計算から生存を主張しない"},
    {"component": "3DCG", "implementation": "保存した位置と姿勢を補間。表示用に飛行状態を書き換えない", "status": "寸法の公表値を参考にした形状。実機CAD/光学モデルではない"},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--profile", type=Path, default=ROOT/"examples/spaceflight/starship-sixdof-profile.json")
    parser.add_argument("--scenario", choices=("all", "launch", "engine_out", "entry_perturbation", "gimbal_step", "flap_asymmetry", "deployment_no_effect", *CATCH_SCENARIOS), default="all")
    parser.add_argument("--supervision-dir", type=Path)
    parser.add_argument("--supervision-request-id")
    parser.add_argument("--collect-observation", action="store_true")
    parser.add_argument("--return-policy", choices=(FIXED_RETURN_POLICY, RETAINED_RETURN_POLICY, "mass_state_terminal_v2", CONTINUOUS_RETURN_POLICY), default=FIXED_RETURN_POLICY)
    parser.add_argument("--booster-policy", choices=(FIXED_RETURN_POLICY, BOOSTER_RECOVERY_POLICY), default=FIXED_RETURN_POLICY)
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--dt-scale", type=float, default=1.)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("output/starship-sixdof"))
    args = parser.parse_args()
    if not args.approve_simulation:
        print(json.dumps({"status": "not_started", "required_flag": "--approve-simulation"}))
        return 2
    if bool(args.supervision_dir) != bool(args.supervision_request_id):
        parser.error("--supervision-dir and --supervision-request-id must be supplied together.")
    if args.supervision_dir and args.scenario != "deployment_no_effect":
        parser.error("The supervision mailbox is only supported by deployment_no_effect.")
    if args.collect_observation and not args.supervision_dir:
        parser.error("Observation collection requires an explicit supervision mailbox.")
    if args.return_policy != FIXED_RETURN_POLICY and args.scenario != "deployment_no_effect":
        parser.error("Retained-payload guidance is only supported by deployment_no_effect.")
    if args.booster_policy != FIXED_RETURN_POLICY and (args.scenario != "launch" or args.dt_scale != 1.):
        parser.error("Launch-connected recovery requires launch with the fixed integration scale.")
    profile_bytes = args.profile.read_bytes()
    p = json.loads(profile_bytes)
    catch_bytes = (ROOT/CATCH_PROFILE).read_bytes()
    catch_profile = json.loads(catch_bytes)
    if args.scenario in CATCH_SCENARIOS and (not math.isfinite(args.dt_scale) or not 0 < args.dt_scale <= 1):
        parser.error("Catch integration requires a finite dt scale in (0, 1].")
    if args.scenario in CATCH_SCENARIOS:
        duration = catch_profile["duration_s"] if args.duration_s is None else args.duration_s
        dt = catch_profile["integration_dt_s"] * args.dt_scale
        if (not math.isfinite(duration) or not 0 < duration <= 30 or dt <= 0
                or duration/dt > 99_999 or 600+dt <= 600 or 600+duration <= 600):
            parser.error("Catch integration requires advancing time and at most 99,999 steps.")
    sources = ["src/runtime/starship_sixdof.py", "src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof_separation.py",
               "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_sixdof_booster.py", "src/runtime/starship_physics.py", "scripts/run_starship_sixdof.py",
               "src/runtime/starship_flight_supervision.py",
               "src/runtime/starship_retained_return.py",
               "src/runtime/starship_attitude_reference.py", "src/runtime/starship_booster_catch.py", CATCH_PROFILE,
               "src/runtime/starship_booster_control.py", "src/runtime/starship_booster_recovery.py",
               "src/runtime/starship_fin_allocation.py",
               "src/runtime/starship_entry_trim.py", "src/runtime/starship_return_feasibility.py",
               "src/runtime/starship_return_feasibility_verifier.py",
               "docs/assets/starship-state-return-qualification/qualification.json",
               "src/runtime/starship_landing_context.py",
               "src/runtime/starship_wind.py",
               "src/runtime/starship_sixdof_report.py", "src/runtime/assets/starship_sixdof_replay.js", "src/runtime/assets/starship_cg_models.js", "src/runtime/assets/starship_cg_renderer.js"]
    source_hashes = {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    profile_hash = sha256(profile_bytes).hexdigest()
    validation_bytes = args.validation.read_bytes() if args.validation else None
    validation = json.loads(validation_bytes) if validation_bytes is not None else None
    if validation is not None:
        if not isinstance(validation, dict) or validation.get("schema") != "missionos.starship_sixdof_validation.v1":
            parser.error("Unsupported validation schema.")
        for name, digest in validation.get("source_sha256", {}).items():
            if Path(name).is_absolute() or ".." in Path(name).parts or not (ROOT/name).is_file() or sha256((ROOT/name).read_bytes()).hexdigest() != digest:
                parser.error("Validation sources do not match this checkout; rerun numerical validation.")
        if not {"src/runtime/starship_sixdof.py", "src/runtime/starship_physics.py"}.issubset(validation.get("source_sha256", {})):
            parser.error("Validation must identify the dynamics and environment sources.")
    scenarios = ["launch", "engine_out", "entry_perturbation", "gimbal_step", "flap_asymmetry"] if args.scenario == "all" else [args.scenario]
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output is not empty; choose a fresh directory to preserve earlier or failed results.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runs = []
    for scenario in scenarios:
        print(json.dumps({"status": "running", "scenario": scenario}), flush=True)
        try:
            supervisor = FlightSupervision(args.supervision_dir, args.supervision_request_id,
                collect_observation=args.collect_observation) if args.supervision_dir else None
            if scenario in CATCH_SCENARIOS:
                from src.runtime.starship_booster_catch import simulate_catch
                run = simulate_catch(p, catch_profile, scenario=scenario, duration_s=args.duration_s,
                                     dt_s=catch_profile["integration_dt_s"] * args.dt_scale)
            else:
                run = simulate(p, scenario=scenario, duration_s=args.duration_s, dt_scale=args.dt_scale,
                               supervision=supervisor, return_policy=args.return_policy,
                               booster_policy=args.booster_policy, catch_config=catch_profile)
            (args.output_dir/f"{scenario}.json").write_text(json.dumps(run, allow_nan=False, separators=(",", ":")))
        except Exception as exc:
            (args.output_dir/"failure.json").write_text(json.dumps({"status": "execution_error", "scenario": scenario,
                "error": f"{type(exc).__name__}: {exc}", "profile": p, "source_sha256": source_hashes}, allow_nan=False))
            raise
        runs.append(run)
        print(json.dumps({"status": "recorded", "scenario": scenario, **{k: run["outcome"][k] for k in (
            "termination", "duration_s", "max_altitude_m", "final_ground_speed_mps", "orbit_gate_reached", "payload_released_count")}}), flush=True)
    checks = []
    if args.validation:
        # Keep the independently produced report intact; no coercion of a
        # skipped native comparison into a pass.
        (args.output_dir/"validation.json").write_bytes(validation_bytes)
        native_invoked = validation.get("runtime_invocation", {}).get("native_basilisk_invoked") is True
        checks.append({"name": "独立数値検証", "passed": validation.get("passed"),
                       "scope": "NASA回転subset・解析解"+("・native Basilisk" if native_invoked else "（Basilisk未実行）")+"。Starship実機検証ではない"})
        native_results = [case["native_basilisk"]["passed"] for case in validation["cases"] if isinstance(case.get("native_basilisk"), dict) and "passed" in case["native_basilisk"]]
        checks.append({"name": "native Basilisk 実呼出し", "passed": all(native_results) if native_invoked and native_results else False if native_invoked else None,
                       "scope": "別積分器による固定質量の位置・速度・姿勢・角速度比較" if native_invoked else "この検証はCPU解析比較のみ。nativeは未実行"})
        for case in validation["cases"]:
            checks.append({"name": case["case_id"], "passed": case.get("passed"), "scope": str(case.get("reference", "解析的・独立数値比較"))})
    else:
        checks.append({"name": "独立数値検証", "passed": None, "scope": "この実行に検証ファイルを添付していません"})
    for run in runs:
        norms = [sum(x*x for x in s["q_body_to_eci"]) for s in run["samples"]]
        checks.append({"name": run["scenario"]+" / quaternion", "passed": all(abs(n-1)<1e-8 for n in norms),
                       "scope": f"{len(norms)} 保存点。姿勢は積分状態。飛行成功の判定ではない"})
        for e in run["events"]:
            if e["event"] == "stage_separation":
                checks.append({"name": run["scenario"]+" / 分離運動量", "passed": abs(e["mass_residual_kg"])<1e-5 and e["linear_momentum_residual_kg_mps"]<.1 and e["angular_momentum_residual_kg_m2_s"]<1.,
                               "scope": "瞬時剛体分離の質量・線形/角運動量。実機ホットステージ検証ではない"})
    if any(sha256((ROOT/name).read_bytes()).hexdigest() != digest for name, digest in source_hashes.items()):
        raise RuntimeError("Sources changed during simulation; saved per-case results are development evidence, not a frozen study.")
    study = {"schema": "missionos.starship_sixdof_study.v1", "title": "STARSHIP / SIX DEGREES OF FREEDOM", "profile": p,
              "runs": runs, "coverage": COVERAGE, "checks": checks, "validation": validation,
              "catch_profile": catch_profile if args.scenario in CATCH_SCENARIOS or args.booster_policy == BOOSTER_RECOVERY_POLICY else None,
             "provenance": {"created_utc": datetime.now(timezone.utc).isoformat(), "wall_time_s": time.monotonic()-started,
                            "dt_scale": args.dt_scale, "duration_override_s": args.duration_s,
                            "return_policy": args.return_policy,
                            "booster_policy": args.booster_policy,
                            "observation_collection_enabled": args.collect_observation,
                            "catch_profile_sha256": sha256(catch_bytes).hexdigest(),
                            "source_sha256": source_hashes,
                            "profile_sha256": profile_hash,
                            "physical_execution_invoked": False, "starship_vehicle_validated": False}}
    (args.output_dir/"study.json").write_text(json.dumps(study, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    (args.output_dir/"report.html").write_text(build_report(study))
    manifest = {"schema": "missionos.starship_sixdof_manifest.v1", "files": {f.name: sha256(f.read_bytes()).hexdigest() for f in sorted(args.output_dir.iterdir()) if f.is_file() and f.name != "manifest.json"}}
    (args.output_dir/"manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": "completed", "report": str(args.output_dir/"report.html"), "wall_time_s": time.monotonic()-started}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

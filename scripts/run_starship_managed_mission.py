#!/usr/bin/env python3
"""Opt-in same-start fixed-timeline / MissionOS six-DOF comparison."""
from __future__ import annotations

import argparse
import gzip
import html
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_mission_director import RESPONSE_FAULTS, SCENARIOS, MissionDirector, contract, source_hashes  # noqa: E402
from src.runtime.starship_mission_director_verifier import comparison, verify  # noqa: E402
from src.runtime.starship_return_sites import ReturnSites  # noqa: E402
from src.runtime.starship_sixdof_catalog import SIXDOF_PROFILE, CATCH_PROFILE  # noqa: E402
from src.runtime.starship_sixdof_mission import simulate  # noqa: E402
from src.runtime.starship_sixdof_report import build_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--case", choices=tuple(sorted(set(SCENARIOS.values()))), required=True)
    parser.add_argument("--response-fault", choices=RESPONSE_FAULTS, help="Explicit fixture-only response failure; never live inference")
    parser.add_argument("--splashdown", action="store_true", help="Separate approved offshore controlled-water-entry goal")
    parser.add_argument("--director-mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--mailbox", type=Path)
    parser.add_argument("--run-id", default="standalone")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_simulation:
        parser.error("Explicit --approve-simulation required")
    if args.director_mode == "live" and not args.mailbox:
        parser.error("Live decisions require the credential-free worker mailbox and host broker")
    if args.response_fault and args.director_mode != "fixture":
        parser.error("Synthetic response faults require fixture mode")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Choose a fresh output directory; failed attempts are preserved")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources = source_hashes(ROOT)
    p = json.loads((ROOT/SIXDOF_PROFILE).read_text())
    catch = json.loads((ROOT/CATCH_PROFILE).read_text())
    sites = ReturnSites.from_dict(json.loads((ROOT/"examples/spaceflight/starship-return-sites-model-test.json").read_text()),
                                 profile=p, catch_config=catch)
    envelope = contract(args.director_mode, splashdown=args.splashdown)
    from src.runtime.starship_splashdown import load_goal
    splashdown_goal = load_goal() if args.splashdown else None
    fixture = None
    if not args.mailbox:
        from src.intelligence.starship_mission_director import fixture_decision
        fixture = fixture_decision
    actor = MissionDirector(envelope, args.mailbox, args.run_id, fixture_decider=fixture, response_fault=args.response_fault)
    runs, started = [], time.monotonic()
    for name, director in (("fixed_timeline", None), ("missionos", actor)):
        print(json.dumps({"status": "running", "controller": name, "case": args.case}), flush=True)
        run = simulate(p, mission_case=args.case, mission_director=director, return_sites=sites, splashdown_goal=splashdown_goal,
            return_policy="fixed_v1" if director is not None else "trimmed_state_terminal_v4")
        # Keep each completed branch recoverable if a later branch fails,
        # without storing an extra uncompressed copy of the full study.
        with gzip.open(args.output_dir/(name+".json.gz"), "wt", encoding="utf-8") as checkpoint:
            json.dump(run, checkpoint, allow_nan=False, separators=(",", ":"))
        runs.append(run)
        print(json.dumps({"status": "recorded", "controller": name, "outcome": run["outcome"]}), flush=True)
    if source_hashes(ROOT) != sources:
        raise ValueError("sources_changed_during_execution")
    study = {"schema": "missionos.starship_managed_study.v1", "case": args.case, "profile": p,
             "response_fault": args.response_fault,
             "return_sites": sites.to_dict(), "envelope": envelope, "runs": runs,
             "comparison": comparison(args.case, *runs), "physical_execution": False,
             "provenance": {"source_sha256": sources, "wall_time_s": time.monotonic()-started,
                "dt_scale": 1., "duration_override_s": None, "physical_execution_invoked": False,
                "starship_vehicle_validated": False, "branch_clock_execution": "independent_ship_then_booster",
                "sensor_model": "bounded_deterministic_gauge_noise_quantization_and_synthetic_ack_tracker_latch_channels"}}
    # Validate the saved JSON representation, as the production worker does.
    # Dataclass tuple fields are not accepted as if they were stored JSON lists.
    study = json.loads(json.dumps(study, allow_nan=False))
    verdict = verify(study, expected_case=args.case, expected_envelope=envelope, expected_run_id=args.run_id,
                     expected_response_fault=args.response_fault)
    (args.output_dir/"study.json").write_text(json.dumps(study, allow_nan=False, separators=(",", ":")))
    (args.output_dir/"verification.json").write_text(json.dumps(verdict, indent=2, allow_nan=False))
    # The replay is the actual managed trajectory; the full pair is in study.json.
    rendered = build_report({**study, "title": "MISSIONOS / MISSION DECISIONS", "runs": [runs[1]],
                             "coverage": [], "checks": [{"name": "保存記録", "passed": verdict["passed"], "scope": "独立した記録検証。比較合格・飛行成功は別判定"}]})
    decisions = "".join("<tr><td>"+html.escape(r["request"]["point"])+"</td><td>"+
        html.escape(str((r["dispatch"] or {}).get("action")))+"</td><td>"+
        html.escape(str((r["dispatch"] or {}).get("time_s")))+"</td><td>"+
        html.escape(str((r["later_observation"] or {}).get("time_s")))+"</td></tr>"
        for r in runs[1]["mission_director"]["records"])
    panel = '<section class="content" id="mission-decisions"><h2>MissionOS / 飛行全体の判断</h2>'
    panel += '<p>飛行前に範囲を承認。範囲内の判断、独立した実行チェック、指令、後続観測を記録します。</p>'
    panel += '<p>'+html.escape(args.director_mode)+' / 比較合格: '+str(study["comparison"]["comparison_accepted"])+'. 合成応答はAI推論ではありません。</p>'
    panel += '<table><tr><th>判断点</th><th>実行</th><th>T+ 指令</th><th>T+ 後続観測</th></tr>'+decisions+'</table>'
    panel += '<details><summary>固定タイムラインとの比較</summary><pre>'+html.escape(json.dumps(study["comparison"], indent=2))+'</pre></details>'
    if args.splashdown:
        water = runs[1]["booster_run"]["splashdown"]
        panel += '<h3>Super Heavy / 制御スプラッシュダウン</h3><p>海側のモデル区域での水面進入条件: '+str(water["controlled_water_entry_envelope_met"])+'. 波浪・浮力・構造・実海域の安全性は未検証。</p><pre>'+html.escape(json.dumps(water,indent=2))+'</pre>'
    panel += '<p>衛星は汎用剛体。ShipとBoosterは独立した時計で順次積分。実時間の並行管制、キャッチ・退避先到達、実機精度、人の作業量削減、AI優位性は未検証です。</p></section>'
    rendered = rendered.replace('</body>', panel+'</body>')
    (args.output_dir/"report.html").write_text(rendered)
    manifest = {"schema": "missionos.starship_managed_manifest.v1", "files": {
        f.name: sha256(f.read_bytes()).hexdigest() for f in sorted(args.output_dir.iterdir()) if f.is_file()}}
    (args.output_dir/"manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": "verified" if verdict["passed"] else "invalid_record", "issues": verdict["issues"],
                      "comparison": study["comparison"], "wall_time_s": time.monotonic()-started}), flush=True)
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

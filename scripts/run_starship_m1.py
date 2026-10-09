#!/usr/bin/env python3
"""Opt-in launch -> deployment -> repeated return control -> measured contact."""

import argparse
from hashlib import sha256
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_replanning import contract, source_hashes  # noqa: E402
from src.runtime.starship_replanning_executor import ReplanningExecutor  # noqa: E402
from src.runtime.starship_replanning_verifier import verify, metrics  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_return_feasibility import readiness  # noqa: E402


def report(study, verdict):
    m = metrics(study["execution"])
    data = {
        "record_verification": verdict,
        "contact": m,
        "scheduled_s": study["scheduled_s"],
        "dispatched_s": None if not study["dispatch"] else study["dispatch"]["time_s"],
        "revisions": [
            {k: v for k, v in r.items() if k in ("revision", "time_s", "candidate_id", "basis")}
            for r in study["plan_revisions"]
        ],
        "model_calls": sum(
            bool((r["response"] or {}).get("model_inference_invoked")) for r in study["decisions"]
        ),
        "single_choice_model_calls": sum(
            bool((r["response"] or {}).get("model_inference_invoked"))
            and len(r["request"]["allowed_actions"]) == 1
            for r in study["decisions"]
        ),
        "multiple_choice_model_calls": sum(
            bool((r["response"] or {}).get("model_inference_invoked"))
            and len(r["request"]["allowed_actions"]) > 1
            for r in study["decisions"]
        ),
        "human_inflight_commands": 0,
        "supervision_events": study.get("supervision_events", []),
        "event_scope": study["envelope"].get("event_supervision"),
    }
    rows = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(x)) + "</td>"
            for x in (
                r["request"]["time_s"],
                r["request"]["stage"],
                r["accepted_action"],
                r["later_time_s"],
                r["fallback"],
            )
        )
        + "</tr>"
        for r in study["decisions"]
    )
    return (
        '<!doctype html><html lang="ja"><meta charset="utf-8"><title>MissionOS / Return replanning</title><style>body{background:#0b1220;color:#e4edf7;font:16px system-ui;max-width:1100px;margin:50px auto}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #34445a}pre{white-space:pre-wrap;background:#152235;padding:20px}a{color:#71b7ff}</style><h1>MissionOS / 帰還の延期と再計画</h1><p>打上げ → 26基放出 → 更新された回収条件 → 有限の待機 → 再計画 → 接触観測</p><pre>'
        + html.escape(json.dumps(data, ensure_ascii=False, indent=2))
        + "</pre><table><tr><th>T+</th><th>判断点</th><th>判断</th><th>後続観測 T+</th><th>代替動作</th></tr>"
        + rows
        + '</table><p>固定した合成区域と運用通知による開発試験。待機の電力・温度・損失は上限の仮定。3点の観測摂動は全状態の安全証明ではありません。実海域の利用許可、SpaceX実機精度、水面進入後の機体状態、回収成功は未検証。</p><p><a href="study.json">全実行記録</a> · <a href="verification.json">独立検証</a></p></html>'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument(
        "--case",
        choices=(
            "normal",
            "recovery_update",
            "decision_timeout",
            "event_normal",
            "event_tradeoff",
            "event_timeout",
        ),
        required=True,
    )
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--mailbox", type=Path)
    parser.add_argument("--run-id", default="standalone")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if (
        not args.approve_simulation
        or args.output_dir.exists()
        or (args.mode == "live" and not args.mailbox)
    ):
        parser.error("explicit opt-in, fresh output and a live host broker are required")
    if args.case in ("decision_timeout", "event_timeout") and args.mode != "fixture":
        parser.error("synthetic timeout is fixture only")
    p = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    _, _, error = readiness(p)
    if error:
        parser.error(error)
    args.output_dir.mkdir(mode=0o700, parents=True)
    envelope, sources = contract(args.mode, args.case), source_hashes()
    write_verified_input(
        args.output_dir / "inputs.json",
        {"case": args.case, "envelope": envelope, "source_sha256": sources},
    )
    for name in sources:
        target = args.output_dir / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    actor = None
    try:
        actor = ReplanningExecutor(
            p, envelope, args.case, args.output_dir, mailbox=args.mailbox, run_id=args.run_id
        )
        study = actor.run()
    except Exception as error:
        write_verified_input(
            args.output_dir / "failure.json", {"type": type(error).__name__, "message": str(error)}
        )
        if actor is None:
            raise
        study = actor.finish()
        study["runtime_failure"] = type(error).__name__ + ":" + str(error)
    study = write_verified_input(args.output_dir / "study.json", study)
    verdict = verify(
        study, expected_envelope=envelope, expected_case=args.case, expected_sources=sources
    )
    if sources != source_hashes() or "runtime_failure" in study:
        verdict["passed"] = False
        verdict["issues"].append("runtime_failure_or_source_change")
    write_verified_input(args.output_dir / "verification.json", verdict)
    (args.output_dir / "report.html").write_text(report(study, verdict))
    manifest = {
        "schema": "missionos.starship_m1_manifest.v1",
        "files": {
            name: sha256((args.output_dir / name).read_bytes()).hexdigest()
            for name in ("study.json", "verification.json", "report.html")
        },
    }
    write_verified_input(args.output_dir / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "verification": verdict,
                "contact": metrics(study["execution"]),
                "forecasts": study["forecast_count"],
                "decisions": len(study["decisions"]),
            }
        ),
        flush=True,
    )
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

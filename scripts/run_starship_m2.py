#!/usr/bin/env python3
"""One opt-in M2 run: actual deployment -> same-state return -> record checks.

Choose one case per fresh directory. Failed/unresolved evidence is preserved;
there is no automatic retry or physical flight authority. Live decisions require
an approved authority and separate host-controlled phase mailboxes.
"""

import argparse
from hashlib import sha256
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_m2_contract import CASES, PROFILE, m2_contract  # noqa: E402
from src.runtime.starship_m2_runner import run_simulation, sources  # noqa: E402
from src.runtime.starship_m2_verifier import verify  # noqa: E402
from src.runtime.starship_replanning import source_hashes as return_sources  # noqa: E402
from src.runtime.starship_replanning_verifier import metrics  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--run-id", default="m2-fixture")
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--authority-file", type=Path)
    parser.add_argument("--mailbox", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("explicit --approve-simulation and fresh output directory required")
    if args.mode == "live" and (args.authority_file is None or args.mailbox is None):
        parser.error("live requires an approved authority file and phase mailboxes")
    authority = (
        json.loads(args.authority_file.read_text())
        if args.authority_file
        else m2_contract(args.mode, args.case)
    )
    from src.runtime.starship_m2_control import validate_worker_input

    validate_worker_input(
        authority, mode=args.mode, case=args.case, run_id=args.run_id, mailbox=args.mailbox
    )
    frozen_sources, inner_sources = sources(), return_sources()
    profile = json.loads((ROOT / PROFILE).read_text())
    study = run_simulation(
        profile,
        args.case,
        args.output_dir,
        run_id=args.run_id,
        mode=args.mode,
        authority=authority,
        mailbox=args.mailbox,
    )
    verdict = verify(study, expected_authority=authority, expected_sources=frozen_sources,
                     expected_return_sources=inner_sources, expected_run_id=args.run_id)
    if frozen_sources != sources() or inner_sources != return_sources():
        verdict["passed"] = verdict["case_accepted"] = False
        verdict["issues"].append("sources_changed_during_invocation")
    write_verified_input(args.output_dir / "verification.json", verdict)
    returning, handoff = study["return_study"], study["handoff"]
    summary = {
        "schema": "missionos.starship_m2_summary.v1",
        "case": args.case,
        "run_id": args.run_id,
        "verification": verdict,
        "released_count": handoff["released_count"] if handoff else None,
        "retained_count": handoff["retained_count"] if handoff else None,
        "contact": metrics(returning["execution"]) if returning else None,
        "ship_return_dispatched": bool(returning and returning["dispatch"]),
        "model_calls": study["runtime"]["model_calls"],
        "mode": args.mode,
        "human_inflight_commands": 0,
        "retry_performed": False,
        "mission_completed": False,
        "physical_execution": False,
    }
    write_verified_input(args.output_dir / "summary.json", summary)
    (args.output_dir / "report.html").write_text(
        '<!doctype html><html lang="ja"><meta charset="utf-8"><title>MissionOS M2</title>'
        "<h1>放出判断の結果から、同じ便の帰還へ</h1><p>開発試験。fixture と実モデル呼出しは記録で区別します。実海域の利用許可・"
        "SpaceX実機精度・機体回収は未検証。記録の整合性と帰還結果は別々に判定します。</p><pre>"
        + html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
        + '</pre><a href="study.json">全記録</a> · <a href="verification.json">独立検証</a></html>'
    )
    write_verified_input(args.output_dir / "manifest.json", {
        "schema": "missionos.starship_m2_manifest.v1",
        "files": {name: sha256((args.output_dir / name).read_bytes()).hexdigest()
                  for name in ("study.json", "verification.json", "summary.json", "report.html")},
    })
    print(json.dumps(summary), flush=True)
    return 0 if verdict["case_accepted"] else 3 if verdict["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

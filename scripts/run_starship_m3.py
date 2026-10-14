#!/usr/bin/env python3
"""One explicitly approved MissionOS M3 flight; no retries or worker secrets."""

import argparse
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_m3_live import SCENARIOS, run, sources, validate_worker, verify  # noqa: E402
from src.runtime.starship_m2_contract import PROFILE  # noqa: E402
from src.runtime.starship_replanning import source_hashes as return_sources  # noqa: E402
from src.runtime.starship_replanning_verifier import metrics  # noqa: E402
from src.runtime.starship_runtime_budget import BudgetExceeded, RuntimeBudget  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--mode", choices=("fixture", "live"), required=True)
    parser.add_argument("--case", choices=tuple(SCENARIOS.values()), required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inflight-binding", type=Path)
    for channel in ("request", "grant", "telemetry"):
        parser.add_argument("--inflight-"+channel+"-fd", type=int)
    args = parser.parse_args(argv)
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("explicit approval and fresh output directory required")
    if any(
        name in os.environ
        for name in (
            "TYPESAFE_API_KEY",
            "DEEPSEEK_API_KEY",
            "JEV_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        )
    ):
        parser.error("provider_credentials_forbidden_in_simulator_worker")
    # Refuse host-only operator credentials in a manually launched worker too.
    # This is an environment rejection, not an environment forwarding list.
    if any(name.startswith("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_") for name in os.environ):
        parser.error("host_operator_credentials_forbidden_in_simulator_worker")
    authority = json.loads(args.authority_file.read_text())
    validate_worker(
        authority, mode=args.mode, case=args.case, run_id=args.run_id, mailbox=args.mailbox
    )
    inflight_args = (args.inflight_binding, args.inflight_request_fd, args.inflight_grant_fd, args.inflight_telemetry_fd)
    enabled = authority["inflight_scope_amendment_enabled"]
    if enabled != all(value is not None for value in inflight_args) or (not enabled and any(value is not None for value in inflight_args)):
        parser.error("inflight_binding_and_three_channels_required")
    guard = RuntimeBudget(args.output_dir, authority)
    try:
        with guard:
            return execute(args, authority)
    except BudgetExceeded:
        # A final budget sample can follow a completed verdict write. Preserve
        # that prior verdict in the failure evidence, then remove stale success.
        verdict_path = args.output_dir / "verification.json"
        if verdict_path.exists():
            try:
                previous = json.loads(verdict_path.read_text())
                guard.failure["previous_verification"] = previous
                invalid = dict(previous)
                invalid.update(passed=False, case_accepted=False, ai_control_demonstrated=False)
                invalid["issues"] = list(previous.get("issues", [])) + [
                    "outer_runtime_budget_exceeded"
                ]
                # Remove the prior success from its authoritative path before
                # creating the negative result; preserve it as historical evidence.
                # Even disk exhaustion cannot leave verification.json claiming pass.
                preserved = args.output_dir / "verification-before-budget-failure.json"
                if preserved.exists():
                    raise ValueError("budget_previous_verdict_already_preserved")
                verdict_path.rename(preserved)
                write_verified_input(verdict_path, invalid)
            except (OSError, ValueError, TypeError):
                guard.failure["previous_verification_unreadable"] = True
        failure = guard.failure_artifact()
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "case_accepted": False,
                    "runtime_failure": failure,
                    "retry_performed": False,
                }
            ),
            flush=True,
        )
        return 4


def execute(args, authority):
    frozen, inner_sources = sources(), return_sources()
    profile = json.loads((ROOT / PROFILE).read_text())
    inflight_runtime, inflight_binding = None, None
    if args.inflight_binding is not None:
        from src.runtime.starship_m3_inflight import worker_runtime
        signed_binding = json.loads(args.inflight_binding.read_text())
        inflight_binding = {key: value for key, value in signed_binding.items() if key != "signature"}
        inflight_runtime = worker_runtime(inflight_binding, authority, run_id=args.run_id, sources=frozen,
            request_fd=args.inflight_request_fd, grant_fd=args.inflight_grant_fd, telemetry_fd=args.inflight_telemetry_fd)
    study = run(
        profile,
        args.case,
        args.output_dir,
        mode=args.mode,
        authority=authority,
        mailbox=args.mailbox,
        run_id=args.run_id, inflight_runtime=inflight_runtime, inflight_binding=inflight_binding,
    )
    host_evidence = None
    if inflight_binding is not None:
        from src.runtime.starship_m3_inflight import await_host_evidence
        host_evidence = await_host_evidence(args.inflight_binding.parent, inflight_binding)
        study["inflight_host_evidence"] = host_evidence
        write_verified_input(args.output_dir / "study.json", study)
    verdict = verify(
        study,
        expected_authority=authority,
        expected_sources=frozen,
        expected_return_sources=inner_sources,
        expected_run_id=args.run_id, expected_inflight_binding=inflight_binding,
        expected_host_evidence=host_evidence,
    )
    if sources() != frozen or return_sources() != inner_sources:
        verdict.update(passed=False, case_accepted=False)
        verdict["issues"].append("sources_changed_during_invocation")
    write_verified_input(args.output_dir / "verification.json", verdict)
    inner = study["m2_study"]
    returning, handoff = inner.get("return_study"), inner.get("handoff")
    summary = {
        "schema": "missionos.starship_m3_control_summary.v1",
        "run_id": args.run_id,
        "mode": args.mode,
        "case": args.case,
        "verification": verdict,
        "released_count": handoff["released_count"] if handoff else None,
        "retained_count": handoff["retained_count"] if handoff else None,
        "ship_contact": metrics(returning["execution"]) if returning else None,
        "model_calls": verdict.get("model_calls"),
        "retry_performed": False,
        "human_inflight_commands": 0,
        "m3_completed": False,
        "physical_execution": False,
    }
    write_verified_input(args.output_dir / "summary.json", summary)
    (args.output_dir / "report.html").write_text(
        '<!doctype html><html lang="ja"><meta charset="utf-8"><title>MissionOS M3</title>'
        "<h1>Jev・DeepSeekによる通し管制試験</h1><p>AI判断の採用、飛行結果、記録検証を別々に表示します。"
        "接触条件は開発モデル内の判定です。実機回収・水中生存性は未検証です。</p><pre>"
        + html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
        + '</pre><a href="study.json">保存記録</a> · <a href="verification.json">独立検証</a></html>'
    )
    write_verified_input(
        args.output_dir / "manifest.json",
        {
            "schema": "missionos.starship_m3_control_manifest.v1",
            "files": {
                name: sha256((args.output_dir / name).read_bytes()).hexdigest()
                for name in ("study.json", "verification.json", "summary.json", "report.html")
            },
        },
    )
    print(json.dumps(summary), flush=True)
    return 0 if verdict["case_accepted"] else 3 if verdict["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

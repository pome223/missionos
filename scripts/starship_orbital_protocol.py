#!/usr/bin/env python3
"""Prepare or run one explicitly approved, fixed two-call orbital protocol replay."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_starship_gateway import _read_secret  # noqa: E402
from src.intelligence.space_ops_investigator import _json  # noqa: E402
from src.intelligence.starship_orbital_protocol_campaign import (  # noqa: E402
    prepare_bundle, run_campaign, validate_bundle,
)
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_replanning import digest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Offline only, preserves previous probe")
    prepare.add_argument("--original-probe", type=Path, required=True)
    prepare.add_argument("--approved-original-sha256", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    run = commands.add_parser("run", help="Two calls, no retry/resume, never a flight")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--approved-bundle-sha256", required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--project", required=True)
    run.add_argument("--secret", default="deepseek-api-key")
    run.add_argument("--enable-live-models", action="store_true")
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("fresh_orbital_protocol_output_required_no_resume")
    if args.command == "prepare":
        bundle = prepare_bundle(_json(args.original_probe.read_bytes()), args.approved_original_sha256)
        args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        stored = write_verified_input(args.output_dir / "bundle.json", bundle)
        write_verified_input(args.output_dir / "summary.json", {
            "schema": "missionos.starship_orbital_protocol_preparation.v1",
            "bundle_sha256": digest(stored),
            "original_probe_sha256": stored["original_probe_sha256"],
            "provider_request_sha256": stored["provider_request_sha256"],
            "policy": stored["policy"], "proposed_campaign": stored["proposed_campaign"],
            "provider_calls": 0, "flight_runs": 0, "dispatch": False,
            "held_out": False, "model_value_claim": False,
        })
        print("Prepared a new developer replay; hosted calls=0; flights=0.")
        print("Bundle SHA256: " + digest(stored))
        return 0
    if not args.enable_live_models:
        parser.error("explicit_live_orbital_protocol_opt_in_required")
    try:
        bundle = validate_bundle(_json(args.bundle.read_bytes()), args.approved_bundle_sha256)
        key = _read_secret(args.project, args.secret)
        result = run_campaign(bundle, args.approved_bundle_sha256, args.output_dir, key=key)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        parser.error("orbital_protocol_campaign_failed_preserve_records_do_not_retry")
    print(f"{result['setting_id']}: {result['valid_responses']}/{result['attempted_calls']} valid")
    print("Outcome: " + result["decision"])
    print("Hosted attempts: " + str(result["attempted_calls"]) + "; flights: 0; retries: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Execute one explicitly approved, non-resumable four-call protocol campaign."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_starship_gateway import _read_secret  # noqa: E402
from src.intelligence.space_ops_investigator import _json  # noqa: E402
from src.intelligence.starship_protocol_campaign import run_campaign, validate_bundle  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--approved-probe-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--secret", default="deepseek-api-key")
    parser.add_argument("--enable-live-models", action="store_true")
    args = parser.parse_args()
    if not args.enable_live_models:
        parser.error("explicit_live_protocol_opt_in_required")
    if args.output_dir.exists():
        parser.error("fresh_campaign_directory_required_no_resume")
    try:
        bundle = validate_bundle(_json(args.probe.read_bytes()), args.approved_probe_sha256)
        key = _read_secret(args.project, args.secret)
        result = run_campaign(bundle, args.approved_probe_sha256, args.output_dir, key=key)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        parser.error("protocol_campaign_failed_preserve_existing_records_do_not_retry")
    for setting in result["settings"]:
        print(f"{setting['setting_id']}: {setting['valid_responses']}/{setting['attempts']} valid")
    print("Outcome: " + result["decision"])
    print("Selected: " + str(result["selected_setting_id"]))
    print(f"Hosted attempts: {result['attempted_calls']}; flights: 0; retries: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

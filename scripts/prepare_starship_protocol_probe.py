#!/usr/bin/env python3
"""Prepare a frozen offline replay bundle; never call a hosted model or fly."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.intelligence.space_ops_investigator import _json  # noqa: E402
from src.intelligence.starship_protocol_probe import prepare  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_replanning import digest  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--decision-index", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("a fresh output directory is required")
    bundle = prepare(_json(args.study.read_bytes()), args.decision_index)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    stored = write_verified_input(args.output_dir / "probe.json", bundle)
    summary = {
        "schema": "missionos.starship_protocol_preparation_summary.v1",
        "probe_sha256": digest(stored),
        "original_wire_verified": stored["original_wire_verified"],
        "original_provider_request_sha256": stored["original_provider_request_sha256"],
        "original_provider_request_bytes": stored["original_provider_request_bytes"],
        "variants": [{k: v[k] for k in ("setting_id", "provider_request_sha256", "provider_request_bytes", "planned_attempts")} for v in stored["variants"]],
        "proposed_campaign": stored["proposed_campaign"],
        "provider_calls": 0, "flight_runs": 0, "dispatch": False,
    }
    write_verified_input(args.output_dir / "summary.json", summary)
    print("Prepared offline replay; hosted calls=0; flights=0; original wire hash matched.")
    print("Probe SHA256: " + summary["probe_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Aggregate explicit M1 study receipts without provider calls or private prose."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_model_diagnostics import summarize_model_diagnostics  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, action="append", required=True,
                        help="Explicit saved study.json, repeat for cumulative accounting")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("a fresh output path is required")
    summary = summarize_model_diagnostics(json.loads(path.read_text()) for path in args.study)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_verified_input(args.output, summary)
    print(json.dumps({"unique_studies": summary["unique_studies"],
                      "providers": summary["providers"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

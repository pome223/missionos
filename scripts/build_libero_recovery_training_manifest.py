#!/usr/bin/env python3
"""Build the recovery candidate manifest and run the known-holdout audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.runtime.libero_recovery_training_manifest import build_training_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", action="append", type=Path, required=True)
    parser.add_argument("--conversion-dir", action="append", type=Path, required=True)
    parser.add_argument("--phase0-record", type=Path, required=True)
    parser.add_argument("--evaluation-holdout-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("libero_recovery_training_manifest_output_exists")
    phase0 = json.loads(args.phase0_record.read_text(encoding="utf-8"))
    holdouts = (
        json.loads(args.evaluation_holdout_manifest.read_text(encoding="utf-8"))
        if args.evaluation_holdout_manifest
        else None
    )
    result = build_training_manifest(
        candidate_dirs=[path.resolve() for path in args.candidate_dir],
        conversion_dirs=[path.resolve() for path in args.conversion_dir],
        phase0_record=phase0,
        evaluation_holdout_manifest=holdouts,
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["leakage_audit"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

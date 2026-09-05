#!/usr/bin/env python3
"""Build exact, training-excluded identities for preregistered holdouts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.runtime.libero_recovery_training_manifest import build_evaluation_holdout_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("libero_recovery_holdout_manifest_output_exists")
    result = build_evaluation_holdout_manifest(
        [path.resolve() for path in args.candidate_dir]
    )
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

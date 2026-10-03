"""Validate recorded native inputs, or explicitly approve one non-executing HTTP pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.runtime.yokohama_native_shadow import preflight, run_shadow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--approve-shadow", action="store_true")
    args = parser.parse_args()
    if args.approve_shadow and args.output_dir is None:
        parser.error("--approve-shadow requires a new --output-dir")
    if args.output_dir is not None and not args.approve_shadow:
        parser.error("--output-dir requires --approve-shadow; omit both for local preflight")
    try:
        result = (run_shadow(args.plan, args.output_dir, approved=True)
                  if args.approve_shadow else preflight(args.plan))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Native shadow rejected: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result.get("valid", result.get("status") == "completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

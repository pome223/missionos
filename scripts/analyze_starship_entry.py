#!/usr/bin/env python3
"""Diagnose saved entry crossing times without running or modifying a simulation."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.runtime.starship_entry_diagnostics import diagnose_entry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-run", type=Path, required=True, help="Saved study.json or a single saved run"
    )
    parser.add_argument(
        "--reference", type=Path, help="Defaults to the study's embedded public_reference"
    )
    parser.add_argument("--scenario", default="flight14_inspired")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New JSON file; an existing file is never overwritten",
    )
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Use a new output path; existing diagnostics must be preserved")
        input_bytes = args.input_run.read_bytes()
        source = json.loads(input_bytes)
        if not isinstance(source, dict):
            raise ValueError("Saved study or run must be an object")
        runs = source.get("runs", [source])
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            raise ValueError("Saved runs must be a list of objects")
        matches = [run for run in runs if run.get("scenario") == args.scenario]
        if len(matches) != 1:
            raise ValueError("The selected scenario must identify exactly one saved run")
        reference_bytes = args.reference.read_bytes() if args.reference else None
        reference = (
            json.loads(reference_bytes)
            if reference_bytes is not None
            else source.get("public_reference")
        )
        if not isinstance(reference, dict):
            raise ValueError(
                "Provide --reference when the saved artifact has no embedded public_reference"
            )
        diagnostic = diagnose_entry(matches[0], reference)
        diagnostic["file_bindings"] = {
            "input_file_sha256": sha256(input_bytes).hexdigest(),
            "reference_file_sha256": sha256(reference_bytes).hexdigest()
            if reference_bytes is not None
            else None,
            "reference_location": "explicit_reference_file"
            if reference_bytes is not None
            else "input_file.public_reference",
            "input_verification_performed": False,
            "scope": "Content bindings only; rerun the study verifier separately for numerical reproduction",
        }
        rendered = json.dumps(diagnostic, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        # Exclusive creation also protects against an output race or an alias of an input.
        with args.output.open("x", encoding="utf-8") as target:
            target.write(rendered)
        print(
            json.dumps(
                {
                    "status": "diagnostic_written",
                    "scenario": args.scenario,
                    "covered_points": diagnostic["covered_points"],
                    "median_offset_s": diagnostic["lower_segment_alignment"]["median_offset_s"],
                    "accuracy_verified": False,
                },
                allow_nan=False,
            )
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps({"status": "diagnostic_failed", "reason": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

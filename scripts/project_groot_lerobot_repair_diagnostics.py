#!/usr/bin/env python3
"""Project published GR00T cohort evidence into five-axis diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.runtime.groot_lerobot_repair_diagnostic_projection import (  # noqa: E402
    project_groot_repair_diagnostics,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cohort_record", type=Path)
    parser.add_argument("publication_record", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        report = project_groot_repair_diagnostics(
            args.cohort_record,
            args.publication_record,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True))
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate a publication-safe LIBERO recovery-training Phase 0 record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.runtime.libero_recovery_training_phase0 import phase0_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.record.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("phase0_object_required")
        summary = phase0_summary(value, repository_root=REPOSITORY_ROOT)
    except (OSError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())

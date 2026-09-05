#!/usr/bin/env python3
"""Convert a raw LIBERO recovery capture into one LeRobot v2 episode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.runtime.libero_recovery_lerobot_v2 import convert_capture


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    result = convert_capture(args.capture_dir.resolve(), args.output_dir.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

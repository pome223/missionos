#!/usr/bin/env python3
"""Verify CPU render pairs and separate training targets from evaluation data."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.yokohama_adaptation import prepare_payload  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--collection", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    value = prepare_payload(args.collection, args.output_dir)
    print(json.dumps(value["qualification"]))


if __name__ == "__main__":
    main()

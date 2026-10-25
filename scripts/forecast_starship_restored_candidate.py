#!/usr/bin/env python3
"""Private credential-free forecast worker; no execution or public run mode."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.runtime.starship_restored_forecast_pool import worker  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True, choices=("nominal", "next_orbit"))
    parser.add_argument("--job-sha256", required=True)
    args = parser.parse_args(argv)
    return worker(args.job_dir, args.candidate_id, args.job_sha256)


if __name__ == "__main__":
    main()

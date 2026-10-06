"""Private direct-worker entrypoint; never enables a mission catalog choice."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_tower_runtime import execute_tower_worker  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--disable-driver", action="store_true", help="Refuse physical driver import/execution after validating the approved run.")
    args = parser.parse_args()
    return execute_tower_worker(args.state_dir, args.run_id, disable_driver=args.disable_driver)


if __name__ == "__main__":
    raise SystemExit(main())

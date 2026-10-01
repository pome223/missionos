"""Explicit Jev-only loopback launcher; Secret Manager -> runtime memory.
No DeepSeek/planner/provider probes. Never print or persist credential values.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from src.intelligence.yokohama_jev_live import BUDGET_ID, LIVE_ENABLED, LiveLedger
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-project")
    parser.add_argument("--secret-name", default="jev-api-key")
    parser.add_argument("--port", type=int, default=8904)
    args = parser.parse_args()
    if not args.secret_project or not 1 <= args.port <= 65535:
        parser.error("Explicit authorized secret project and valid port required")
    if not LIVE_ENABLED:
        raise SystemExit("Public demonstration live grant is closed; no credential loader or API")
    with LiveLedger().connect():
        pass
    result = subprocess.run(["gcloud", "--project=" + args.secret_project, "secrets",
                             "versions", "access", "latest", "--secret=" + args.secret_name],
                            capture_output=True, timeout=30)
    if result.returncode or not result.stdout.strip():
        raise SystemExit("Jev credential unavailable; server not started; API/flight 0")
    os.environ["TYPESAFE_API_KEY"] = result.stdout.decode().strip()
    del result
    os.environ.update(MISSIONOS_YOKOHAMA_JEV_MODE="live",
                      MISSIONOS_YOKOHAMA_JEV_BUDGET_ID=BUDGET_ID,
                      MISSIONOS_YOKOHAMA_MAP_BACKEND="sitl",
                      MISSIONOS_YOKOHAMA_MAP_PORT=str(args.port),
                      MISSIONOS_YOKOHAMA_MAP_DB=str(ROOT / "output" / ("jev-live-" + BUDGET_ID) / "tasks.db"),
                      MISSIONOS_YOKOHAMA_MAP_OUTPUT_ROOT=str(ROOT / "output" / ("jev-live-" + BUDGET_ID) / "flights"),
                      MISSIONOS_YOKOHAMA_ALTITUDE_DIAGNOSTICS="1",
                      RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM="1")
    try:
        runpy.run_path(str(ROOT / "scripts/yokohama_map_server.py"), run_name="__main__")
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)


if __name__ == "__main__":
    main()

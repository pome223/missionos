#!/usr/bin/env python3
"""One opt-in Jev connection probe, loading its key only into this process."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.start_starship_gateway import _read_secret  # noqa: E402
from src.intelligence.starship_mission_planner import (  # noqa: E402
    StarshipPlannerError, probe_starship_jev,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-live-connection-check", action="store_true")
    parser.add_argument("--project", required=True)
    parser.add_argument("--secret", default="jev-api-key")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.approve_live_connection_check:
        parser.error("explicit live connection check opt-in required")
    if any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None for item in (args.project, args.secret)):
        parser.error("invalid secret locator")
    try:
        os.environ["TYPESAFE_API_KEY"] = _read_secret(args.project, args.secret)
        os.environ["MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED"] = "1"
        result = probe_starship_jev()
    except StarshipPlannerError as error:
        result = {"reason": error.reason, "invocation": error.invocation}
    except Exception:
        result = {"reason": "starship_jev_credential_or_probe_unavailable"}
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ.pop("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result.get("invocation", {}).get("status") == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())

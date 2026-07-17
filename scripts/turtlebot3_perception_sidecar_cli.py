#!/usr/bin/env python3
"""CLI entrypoint for the TurtleBot3 VLM perception sidecar (issue #31).

Classifies one camera frame into a camera_observation payload
(claim_kind, source_frame_ref, confidence) and prints it as JSON. Backend
selection is env-driven, matching turtlebot3_recovery_planner.py: set
MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_ADK_ENABLED=1 for live Gemini, or
MISSIONOS_TURTLEBOT3_PERCEPTION_SIDECAR_COMMAND for an operator-provided
command override.

Output prints the full sidecar result (schema_version, sidecar_status,
blocking_reasons, camera_observation, llm_invocation_evidence) so a bridge
integration can inspect why classification failed, not just the claim.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.intelligence.turtlebot3_perception_sidecar import (
    run_turtlebot3_perception_sidecar,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-path",
        required=True,
        help="Path to a camera frame (PNG) to classify.",
    )
    args = parser.parse_args()

    result = run_turtlebot3_perception_sidecar(image_path=args.image_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["sidecar_status"] == "classified" else 1


if __name__ == "__main__":
    sys.exit(main())

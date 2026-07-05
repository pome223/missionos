#!/usr/bin/env python3
"""Emit a malformed TurtleBot3 recovery proposal for guardrail smoke tests.

This fixture intentionally claims dispatch authority and invents an observation.
MissionOS must reject the proposal and fall back to its deterministic recovery
proposal. It is not a MissionOS dispatch path.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    prompt = json.loads(sys.stdin.read() or "{}")
    obstacle = prompt.get("obstacle_scenario")
    obstacle = obstacle if isinstance(obstacle, dict) else {}
    print(
        json.dumps(
            {
                "selected_action": "avoid_obstacle",
                "reason": "Malformed fixture claims authority and invents telemetry.",
                "dispatch_request_sent": True,
                "input_observations": {
                    "runtime_obstacle_observed": obstacle.get(
                        "runtime_obstacle_observed"
                    ),
                    "fabricated_distance_to_home_m": 123.456,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Start the pinned public GR00T N1.5 policy service on loopback.

This process performs model inference only. It does not dispatch to a robot,
observe motion, or claim task completion.
"""

from __future__ import annotations

import argparse

from gr00t.eval.robot import RobotInferenceServer
from gr00t.experiment.data_config import load_data_config
from gr00t.model.policy import Gr00tPolicy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5567)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    data_config = load_data_config("fourier_gr1_arms_only")
    policy = Gr00tPolicy(
        model_path="nvidia/GR00T-N1.5-3B",
        modality_config=data_config.modality_config(),
        modality_transform=data_config.transform(),
        embodiment_tag="gr1",
        denoising_steps=4,
        device="cuda",
    )
    server = RobotInferenceServer(
        policy,
        host="127.0.0.1",
        port=args.port,
        api_token=None,
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Register a frozen PX4 RGB-D capture on CPU; no model or vehicle commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.prediction.px4_camera import register_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--airframe-sdf", type=Path)
    parser.add_argument("--camera-sdf", type=Path)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        parser.error("specify both --width and --height, or keep the native RGB grid")
    manifest = register_capture(
        args.capture,
        airframe_sdf=args.airframe_sdf or args.capture.parent / "airframe.sdf",
        camera_sdf=args.camera_sdf or args.capture.parent / "camera.sdf",
        output_dir=args.output_dir,
        output_size=None if args.width is None else (args.width, args.height),
    )
    print(json.dumps({
        "registration": str(args.output_dir / "registration.json"),
        "frames": len(manifest["frames"]),
        "source_kind": manifest["source_kind"],
        "model_input_ready": manifest["model_input_ready"],
        "model_inference_invoked": False,
        "dispatch_authority_created": False,
    }))


if __name__ == "__main__":
    main()

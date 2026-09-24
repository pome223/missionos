#!/usr/bin/env python3
"""Render observed simulator RGB at 4x simulation speed; inference waits excluded."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.verify_urban_wam_trial import verify  # noqa: E402


def render(roots, output):
    from PIL import Image, ImageDraw, ImageFont

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=20)
    small = ImageFont.load_default(size=15)
    with tempfile.TemporaryDirectory(prefix="urban-video-") as temporary:
        target = Path(temporary)
        number = 0
        for root in roots:
            result = verify(root)
            session = root / "session"
            events = [
                json.loads(s)
                for s in (session / "events.jsonl").read_text().splitlines()
            ]
            first = next(e for e in events if e["event"] == "urban_route_dispatch")[
                "observed_start"
            ]["pose_simulation_time_ns"]
            last = next(e for e in events if e["event"] == "urban_goal_observed")[
                "observed"
            ]["pose_simulation_time_ns"]
            frames = json.loads((session / "route-images/frames.json").read_text())[
                "frames"
            ]
            frames = [f for f in frames if first <= f["simulation_time_ns"] <= last]
            step = int(4e9 / 16)  # 16 video fps, 4x simulation-time playback.
            for stamp in range(first, last + 1, step):
                frame = min(frames, key=lambda f: abs(f["simulation_time_ns"] - stamp))
                canvas = Image.new("RGB", (960, 680), "#f4f6f9")
                draw = ImageDraw.Draw(canvas)
                mode = (
                    "ANWM ranking + geometry constraints"
                    if result["model_invoked"]
                    else "Geometric route"
                )
                if (
                    result["model_invoked"]
                    and not result["model_selection"]["multiple_admissible_routes"]
                ):
                    mode = "Only one route admitted by geometry"
                draw.text(
                    (20, 12),
                    f"PX4 SITL | {result['family']} | {result['route_id']}",
                    font=font,
                    fill="#15293a",
                )
                draw.text(
                    (20, 40),
                    mode + " | 4x sim time; inference wait excluded",
                    font=small,
                    fill="#475567",
                )
                canvas.paste(
                    Image.open(session / "route-images" / frame["file"]).resize(
                        (960, 540)
                    ),
                    (0, 70),
                )
                draw.text(
                    (20, 616),
                    f"Observed RGB | altitude {frame['pose_enu_m'][2]:.2f} m | sim t={(frame['simulation_time_ns']-first)/1e9:.2f} s",
                    font=small,
                    fill="#15293a",
                )
                draw.text(
                    (20, 642),
                    "Assets: OSRF gazebo_models / Apartment (CC BY 3.0), Nathan Koenig / Cole Biesemeyer",
                    font=ImageFont.load_default(size=12),
                    fill="#475567",
                )
                draw.text(
                    (20, 660),
                    "https://github.com/osrf/gazebo_models | Static simulation; learned navigation benefit unproven",
                    font=ImageFont.load_default(size=12),
                    fill="#475567",
                )
                canvas.save(target / f"{number:05d}.png")
                number += 1
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                "16",
                "-i",
                str(target / "%05d.png"),
                "-c:v",
                "libx264",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.root, args.output)


if __name__ == "__main__":
    main()

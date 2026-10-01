#!/usr/bin/env python3
"""Render recorded RGB with labeled CPU state predictions; never synthetic imagery."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_yokohama_pad_state import load_data  # noqa: E402
from scripts.yokohama_pad_state import write  # noqa: E402


TITLES = {
    "state-depart": "先行機が退出する",
    "state-stall": "途中で停止してから退出",
    "state-reenter": "戻ってきて再び退出",
}


def render(bundle):
    rows = json.loads((bundle / "evaluation/predictions.json").read_text())
    sequences = load_data(bundle / "test-data", {"unseen_test"})
    (bundle / "observed").mkdir(exist_ok=True)
    (bundle / "videos").mkdir(exist_ok=True)
    cases = []
    font = ImageFont.load_default(size=18)
    for sequence in sequences:
        name = sequence["id"]
        subset = [r for r in rows if r["case"] == name]
        items = []
        for row in subset:
            image = "observed/" + name + "-" + str(row["frame_index"]) + ".png"
            Image.fromarray(sequence["rgb"][row["frame_index"]]).save(bundle / image)
            items.append(
                dict(
                    elapsed_s=row["elapsed_s"],
                    image=image,
                    prediction=row["forecast"]["forecasts"],
                    actual_xyz=row["actual_xyz"],
                    actual_states=row["actual_states"],
                    supported=row["forecast"]["supported"],
                    detection=row["forecast"]["detections"][-1],
                    advisory=row["advisory"],
                    latency_s=row["forecast"]["compute_seconds"],
                )
            )
        cases.append(dict(id=name, title=TITLES[name], rows=items, video="videos/" + name + ".mp4"))
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            "1000x620",
            "-framerate",
            "4",
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(bundle / "videos" / (name + ".mp4")),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        for i, rgb in enumerate(sequence["rgb"]):
            current = [r for r in subset if r["frame_index"] <= i and i - r["frame_index"] < 4]
            row = current[-1] if current else None
            im = Image.new("RGB", (1000, 620), "#101d24")
            im.paste(Image.fromarray(rgb).resize((640, 360)), (20, 76))
            d = ImageDraw.Draw(im)
            d.text(
                (20, 20), f"MissionOS | CPU lead-state learning | {name}", font=font, fill="#edf5f7"
            )
            d.text(
                (20, 48),
                f"Measured Gazebo RGB - elapsed {sequence['elapsed_s'][i]:.2f} sim s",
                font=font,
                fill="#aec4cf",
            )
            d.text((680, 88), "4-second position forecast", font=font, fill="#edf5f7")
            d.text((680, 118), "teal: learned / amber: actual", font=font, fill="#aec4cf")
            d.rectangle((680, 153, 974, 430), outline="#49616d")
            # Recorded truth appears only in the comparison view, never inference.
            if row:

                def pt(point):
                    return (695 + float(point[0]) * 19, 414 - float(point[2]) * 34)

                actual = [pt(v) for v in row["actual_xyz"]]
                d.line(actual, fill="#ffc58a", width=3)
                if row["forecast"]["supported"]:
                    pred = [pt(f["xyz_relative_to_pad_m"]) for f in row["forecast"]["forecasts"]]
                    d.line(pred, fill="#8fddd0", width=3)
                    for p in (pred[0], pred[-1]):
                        d.ellipse((p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4), fill="#8fddd0")
                    # This marker belongs to the last observation, not an updated detector call.
                    if i == row["frame_index"]:
                        u, v = row["forecast"]["detections"][-1]["uv"]
                        u, v = 20 + 2 * u, 76 + 2 * v
                        d.ellipse((u - 12, v - 12, u + 12, v + 12), outline="#8fddd0", width=2)
                text = (
                    "WAIT / REOBSERVE"
                    if row["advisory"] == "wait_reobserve"
                    else "ENTRY MAY BE REVIEWED"
                )
                d.text((20, 467), text + " - advisory only", font=font, fill="#8fddd0")
                d.text(
                    (20, 500),
                    f"Forecast from {row['elapsed_s']:.2f} sim s; supported: {row['forecast']['supported']}",
                    font=font,
                    fill="#edf5f7",
                )
            else:
                d.text(
                    (20, 467),
                    "No complete scored history / target window at this time",
                    font=font,
                    fill="#ffc58a",
                )
            d.text((680, 440), "x: along corridor / y: height", font=font, fill="#aec4cf")
            d.text(
                (20, 550),
                "Fixed camera, scripted lead, one known corridor. No ANWM/VLA calls or flight dispatch.",
                font=font,
                fill="#aec4cf",
            )
            d.text(
                (20, 580),
                "Yokohama / Project PLATEAU (CC BY 4.0), modified. Battery telemetry not recorded.",
                font=font,
                fill="#aec4cf",
            )
            process.stdin.write(im.tobytes())
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Video encoding failed")
    write(bundle / "view-data.json", cases)
    template = Path(__file__).with_name("yokohama_pad_state_report.html").read_text()
    report = json.loads((bundle / "evaluation/evaluation.json").read_text())
    metric = report["totals"]["learned"]
    tokens = dict(
        DATA=json.dumps(cases, ensure_ascii=False),
        MATCH=f"{100 * metric['known_match_fraction']:.1f}%",
        FALSE=str(metric["false_clear"]),
        UNKNOWN=f"{100 * metric['unknown_fraction']:.1f}%",
        LATENCY=f"{metric['mean_compute_seconds']:.3f} s",
    )
    for key, value in tokens.items():
        template = template.replace("{{" + key + "}}", value)
    (bundle / "index.html").write_text(template)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    render(p.parse_args().bundle)

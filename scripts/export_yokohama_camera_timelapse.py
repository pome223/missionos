"""Offline export of one run's recorded cameras; no flight, API or interpolation."""
from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess


def load_frames(run: Path, name: str) -> list[dict]:
    frames = json.loads((run / name).read_text())
    if not isinstance(frames, list) or len(frames) < 2:
        raise ValueError("Insufficient recorded frames")
    previous = -math.inf
    for row in frames:
        image = row["image"]
        stamp = image["sensor_sim_s"]
        path = (run / image["file"]).resolve()
        if not math.isfinite(stamp) or stamp < 0 or stamp <= previous:
            raise ValueError("Camera timestamps must be finite and strictly increasing")
        if not path.is_relative_to(run.resolve()):
            raise ValueError("Camera path outside this run")
        if hashlib.sha256(path.read_bytes()).hexdigest() != image["sha256"]:
            raise ValueError("Camera image hash mismatch")
        previous = stamp
    return frames


def prior_frame(frames: list[dict], stamp: float, max_age: float = 2) -> dict | None:
    index = bisect_right([f["image"]["sensor_sim_s"] for f in frames], stamp) - 1
    if index < 0:
        return None
    frame = frames[index]
    return frame if stamp - frame["image"]["sensor_sim_s"] <= max_age else None


def export(run: Path, output: Path, *, speed: float = 12, max_bytes: int = 8 * 1024**2):
    from PIL import Image, ImageDraw, ImageFont

    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Positive finite playback speed required")
    onboard = load_frames(run, "video-frames.json")
    queue = load_frames(run, "queue-video-frames.json")
    cargo = load_frames(run, "payload-video-frames.json")
    output.mkdir(parents=True, exist_ok=False)
    generated = output / "rendered-frames"
    generated.mkdir()
    font, small = ImageFont.load_default(size=16), ImageFont.load_default(size=13)
    durations, provenance, concat = [], [], []
    for i, frame in enumerate(onboard):
        image = frame["image"]
        stamp = image["sensor_sim_s"]
        interval = (onboard[i + 1]["image"]["sensor_sim_s"] - stamp
                    if i + 1 < len(onboard) else 0)
        duration = interval / speed if interval else 1 / 24
        durations.append(duration)
        with Image.open(run / image["file"]) as original:
            canvas = Image.new("RGB", (640, 440), "#102b33")
            canvas.paste(original.convert("RGB"), (0, 80))
        draw = ImageDraw.Draw(canvas)
        draw.text((12, 6), f"CPU PX4/Gazebo | Recorded cameras | {speed:g}x | SIM {stamp:.3f}s",
                  fill="#8feac9", font=font)
        draw.text((12, 30), "City VLA/WAM fixtures; run used one real Jev decision",
                  fill="white", font=small)
        gap = "SOURCE GAP" if interval > 3 else "Camera sampling"
        draw.text((12, 53), f"{gap}: next image +{interval:.3f} sim s; held frames, no interpolation",
                  fill="#ffd07c", font=small)
        inset, label = prior_frame(cargo, stamp), "Delivery camera"
        if inset is None:
            inset, label = prior_frame(queue, stamp), "Pad queue camera"
        if inset is not None:
            meta = inset["image"]
            with Image.open(run / meta["file"]) as original:
                small_image = original.convert("RGB").resize((256, 144))
                canvas.paste(small_image, (380, 290))
            draw.rectangle((378, 268, 638, 438), outline="#8feac9", width=2)
            draw.text((384, 271), f"{label} | SIM {meta['sensor_sim_s']:.2f}s",
                      fill="white", font=small)
        target = generated / f"frame-{i:04d}.png"
        canvas.save(target)
        # Relative generated paths only; no source path is embedded in public receipts.
        concat.extend([f"file 'rendered-frames/{target.name}'", f"duration {duration:.9f}"])
        provenance.append({"sensor_sim_s": stamp, "camera_image_sha256": image["sha256"],
                           "next_sample_sim_seconds": interval,
                           "inset_sha256": inset["image"]["sha256"] if inset else None,
                           "inset_sensor_sim_s": inset["image"]["sensor_sim_s"] if inset else None})
    concat.append(f"file 'rendered-frames/{target.name}'")
    listing = output / "frames.txt"
    listing.write_text("\n".join(concat) + "\n")
    video = output / "camera-timelapse.mp4"
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "1", "-i", str(listing), "-an", "-map_metadata", "-1",
                    "-vf", "fps=24,format=yuv420p", "-c:v", "libx264", "-b:v", "420k",
                    "-maxrate", "480k", "-bufsize", "960k", "-preset", "medium",
                    "-movflags", "+faststart", str(video)], check=True, timeout=300)
    if video.stat().st_size > max_bytes:
        raise ValueError("Encoded video exceeds publication limit; do not upload")
    probe = json.loads(subprocess.check_output([
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(video)], text=True))
    stream = probe["streams"][0]
    if stream["codec_name"] != "h264" or stream["pix_fmt"] != "yuv420p":
        raise ValueError("Unexpected codec")
    receipt = {"schema": "missionos.recorded-camera-timelapse.v1", "speed": speed,
               "source_kind": "recorded_simulator_camera_images", "interpolation": False,
               "audio": False, "source_frame_count": len(onboard),
               "source_start_sim_s": onboard[0]["image"]["sensor_sim_s"],
               "source_end_sim_s": onboard[-1]["image"]["sensor_sim_s"],
               "duration_s": float(probe["format"]["duration"]),
               "width": stream["width"], "height": stream["height"],
               "codec": stream["codec_name"], "pixel_format": stream["pix_fmt"],
               "encoded_frame_count": int(stream["nb_frames"]),
               "size_bytes": video.stat().st_size,
               "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
               "maximum_source_gap_sim_s": max(p["next_sample_sim_seconds"] for p in provenance),
               "frames": provenance}
    (output / "export.json").write_text(json.dumps(receipt, indent=2) + "\n")
    shutil.copyfile(generated / f"frame-{len(onboard) // 2:04d}.png", output / "preview.png")
    print(json.dumps({k: v for k, v in receipt.items() if k != "frames"}))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speed", type=float, default=12)
    args = parser.parse_args()
    export(args.run.resolve(), args.output.resolve(), speed=args.speed)


if __name__ == "__main__":
    main()

"""Make a time-scaled video from retained actual simulator camera frames."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
rows = json.loads((a.run / "video-frames.json").read_text())
rows = sorted(rows, key=lambda x: x["image"]["sensor_sim_s"])
frames = []
for r in rows:
    item = r["image"]
    source = a.run / item["file"]
    data = source.read_bytes()
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    if frames and item["sensor_sim_s"] <= frames[-1]["image"]["sensor_sim_s"]:
        continue
    frames.append(r)
assert len(frames) > 1
concat = a.run / "native-video-concat.txt"
lines = []
for i, r in enumerate(frames):
    path = (a.run / r["image"]["file"]).resolve()
    assert "'" not in str(path)
    duration = (
        (frames[i + 1]["image"]["sensor_sim_s"] - r["image"]["sensor_sim_s"]) / 8
        if i + 1 < len(frames)
        else 0.25
    )
    lines.extend([f"file '{path}'", f"duration {duration:.9f}"])
lines.append(f"file '{(a.run / frames[-1]['image']['file']).resolve()}'")
concat.write_text("\n".join(lines) + "\n")
a.output.mkdir(parents=True, exist_ok=True)
out = a.output / "onboard-timelapse.mp4"
subprocess.run(
    [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-safe",
        "0",
        "-f",
        "concat",
        "-i",
        str(concat),
        "-an",
        "-vf",
        "fps=24,format=yuv420p",
        "-c:v",
        "libx264",
        "-crf",
        "22",
        "-preset",
        "medium",
        "-movflags",
        "+faststart",
        str(out),
    ],
    check=True,
)
meta = {
    "schema": "missionos.observed-camera-timelapse.v1",
    "run_id": json.loads((a.run / "config.json").read_text())["run_id"],
    "source": "actual CPU-rendered Gazebo onboard RGB; no model predictions",
    "frames": len(frames),
    "playback_speed": 8,
    "source_start_sim_s": frames[0]["image"]["sensor_sim_s"],
    "source_end_sim_s": frames[-1]["image"]["sensor_sim_s"],
    "source_frame_manifest_sha256": hashlib.sha256(
        (a.run / "video-frames.json").read_bytes()
    ).hexdigest(),
    "video_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    "video_bytes": out.stat().st_size,
}
(a.output / "video-metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
print(json.dumps(meta))

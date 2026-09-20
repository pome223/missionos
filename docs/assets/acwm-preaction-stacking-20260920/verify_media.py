#!/usr/bin/env python3
"""Decode the published media with ffprobe when FFmpeg is installed."""

from pathlib import Path
import json
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
ffprobe = shutil.which("ffprobe")
if ffprobe is None:
    raise SystemExit("ffprobe is required for media decode verification")

for path in sorted(ROOT.glob("*.mp4")):
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,nb_frames", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert int(stream["width"]) == 480 and int(stream["height"]) == 264
    assert int(stream["nb_frames"]) == 37
    print(f"PASS {path.name}: h264 480x264, 37 frames")

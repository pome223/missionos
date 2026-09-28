#!/usr/bin/env python3
"""Export recorded simulator camera frames with timestamp-bound battery telemetry.

The original images remain unmodified. A separate header is added; missing or
stale telemetry is never filled from a future sample. This is not energy metering.
Requires Pillow and ffmpeg, runs offline, and does not start a simulator or GPU.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scalar(raw, name):
    match = re.search(r"^\s*" + re.escape(name) + r":\s*([^\s]+)", raw, re.M)
    if not match:
        return None
    try:
        value = float(match[1])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def battery_samples(rows, run_id, world_sha):
    samples = {}
    for row in rows:
        if row.get("run_id") != run_id or row.get("world_sha256") != world_sha:
            raise ValueError("Foreign flight telemetry")
        raw = row.get("raw_px4", {}).get("battery_status", "")
        timestamp = scalar(raw, "timestamp")
        if timestamp is None:
            continue
        if not math.isfinite(row["sim_s"]):
            raise ValueError("Invalid simulator timestamp")
        stamp = timestamp / 1e6
        if abs(stamp - row["sim_s"]) > 2:
            raise ValueError("PX4 battery and Gazebo clocks are not aligned")
        connected = re.search(r"^\s*connected:\s*True\s*$", raw, re.M) is not None
        remaining, voltage = scalar(raw, "remaining"), scalar(raw, "voltage_v")
        valid = connected and remaining is not None and 0 <= remaining <= 1
        valid = valid and voltage is not None and voltage > 0
        samples[stamp] = dict(
            sensor_sim_s=stamp,
            remaining_fraction=remaining if valid else None,
            voltage_v=voltage if valid else None,
            valid=bool(valid),
            phase=row["phase"],
            raw_battery_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        )
    return sorted(samples.values(), key=lambda x: x["sensor_sim_s"])


def align_battery(samples, frame_sim_s, max_age_s=2):
    times = [x["sensor_sim_s"] for x in samples]
    idx = bisect_right(times, frame_sim_s) - 1
    if idx < 0:
        return dict(available=False, reason="no prior sample")
    sample = samples[idx]
    age = frame_sim_s - sample["sensor_sim_s"]
    if age > max_age_s or not sample["valid"]:
        return dict(available=False, reason="stale or invalid", age_sim_s=age)
    return dict(available=True, age_sim_s=age, **sample)


def align_wind(receipts, frame_sim_s, run_id, world_sha):
    """Display confirmed command history, never a forecast or measured local flow."""
    prior = []
    for r in receipts:
        if r.get("run_id") != run_id or r.get("world_sha256") != world_sha:
            raise ValueError("Foreign wind receipt")
        if r["end_sim_s"] <= frame_sim_s:
            prior.append(r)
    if not prior:
        return dict(label="WIND SEED 0.0 m/s E | calm before activation", gust_id=None)
    r = max(prior, key=lambda r: r["end_sim_s"])
    if r.get("confirmed") is not True:
        return dict(label="WIND SEED UNCONFIRMED | trial failed", gust_id=None)
    gust = f"GUST #{r['gust_id']}" if r.get("gust_id") is not None else "steady"
    return dict(
        label=f"WIND SEED {r['requested_enu_mps'][0]:.1f} m/s E | {gust} | {r['zone']}",
        gust_id=r.get("gust_id"),
        transition_sequence=r["sequence"],
        confirmation_sim_s=r["end_sim_s"],
        requested_enu_mps=r["requested_enu_mps"],
    )


def recovery_timeline(events, rows):
    """Conservatively date events at the next recorded telemetry sample."""
    labels = {
        "city_attempt_revoked": "INTERRUPTED | old decision revoked",
        "city_recovery_started": "AP HOLD | waiting for stability",
        "city_recovery_held": "AP STABLE | capture fresh observations",
        "city_segment_dispatched": "AP EXECUTING | authorized new segment",
        "city_segment_arrived": "ARRIVAL OBSERVED | AP hold",
        "city_session_revoked": "MODEL SESSION REVOKED",
        "pad_occupied_reported": "AIRCRAFT REPORT | delivery pad occupied",
        "pad_wait_executed": "MISSIONOS: WAIT | aircraft AP hold",
        "pad_entry_authorized": "CLEAR OBSERVED | fresh Rules check passed",
        "pad_entry_dispatch_checked": "MISSIONOS: CONTINUE | AP approach",
        "payload_received": "CARGO RECEIPT VERIFIED | return permitted",
        "landing_observed": "SHIP RETURN | landed and disarmed",
    }
    timeline = []
    index = 0
    for event in events:
        if event["event"] not in labels:
            continue
        while index < len(rows) and rows[index]["wall_s"] < event["wall_s"]:
            index += 1
        if index < len(rows):
            timeline.append(
                dict(
                    sim_s=rows[index]["sim_s"],
                    label=labels[event["event"]],
                    event_sha256=hashlib.sha256(
                        json.dumps(event, sort_keys=True).encode()
                    ).hexdigest(),
                )
            )
    return timeline


def align_recovery(timeline, frame_sim_s):
    prior = [event for event in timeline if event["sim_s"] <= frame_sim_s]
    return prior[-1] if prior else dict(label="AP ROUTE | mission event not observed yet")


def build(run, output, *, camera="onboard", speed=8):
    from PIL import Image, ImageDraw, ImageFont

    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Playback speed must be positive")
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((run / "config.json").read_text())
    rows = [json.loads(s) for s in (run / "flight-trajectory.jsonl").read_text().splitlines()]
    samples = battery_samples(rows, config["run_id"], config["world"]["world_sha256"])
    wind_path = run / "wind-transitions.json"
    wind_receipts = json.loads(wind_path.read_text()) if wind_path.exists() else None
    event_path = run / "flight-events.jsonl"
    recovery = None
    if config.get("decisions", {}).get("hold_recovery") or config["world"].get("pad_queue"):
        events = [json.loads(line) for line in event_path.read_text().splitlines()]
        recovery = recovery_timeline(events, rows)
    manifest = (
        run
        / {
            "onboard": "video-frames.json",
            "cargo": "payload-video-frames.json",
            "queue": "queue-video-frames.json",
        }[camera]
    )
    frames = sorted(json.loads(manifest.read_text()), key=lambda r: r["image"]["sensor_sim_s"])
    frames = list({r["image"]["sensor_sim_s"]: r for r in frames}.values())
    if len(frames) < 2:
        raise ValueError("Not enough recorded camera frames")
    font = ImageFont.load_default(size=18)
    small = ImageFont.load_default(size=14)
    aligned = []
    with tempfile.TemporaryDirectory(prefix="missionos-battery-") as tmp:
        tmp = Path(tmp)
        concat = []
        for i, frame in enumerate(frames):
            meta = frame["image"]
            source = (run / meta["file"]).resolve()
            if not source.is_relative_to(run.resolve()) or sha(source) != meta["sha256"]:
                raise ValueError("Camera frame path or hash mismatch")
            stamp = meta["sensor_sim_s"]
            if not math.isfinite(stamp) or stamp < 0:
                raise ValueError("Invalid camera timestamp")
            battery = align_battery(samples, stamp)
            wind = (
                align_wind(wind_receipts, stamp, config["run_id"], config["world"]["world_sha256"])
                if wind_receipts is not None
                else None
            )
            aligned.append(
                dict(frame_sim_s=stamp, frame_sha256=meta["sha256"], battery=battery, wind=wind)
            )
            recovery_state = align_recovery(recovery, stamp) if recovery is not None else None
            if recovery_state is not None:
                aligned[-1]["recovery"] = recovery_state
            with Image.open(source) as original:
                header = 112 if wind is not None else 88
                if recovery_state is not None:
                    header += 24
                canvas = Image.new("RGB", (original.width, original.height + header), "#102b33")
                canvas.paste(original.convert("RGB"), (0, header))
                d = ImageDraw.Draw(canvas)
                value = (
                    f"{100 * battery['remaining_fraction']:.1f}%  |  {battery['voltage_v']:.2f} V"
                    if battery["available"]
                    else "UNAVAILABLE"
                )
                y = 0
                d.text(
                    (12, y + 7),
                    f"SITL BATTERY  {value}   |   SIM {stamp:.1f}s",
                    font=font,
                    fill="#8feac9",
                )
                d.text(
                    (12, y + 34),
                    "Time-based simulation / current & energy unavailable",
                    font=small,
                    fill="white",
                )
                d.text(
                    (12, y + 57),
                    f"{battery.get('phase', 'NO TELEMETRY')}  |  {speed:g}x playback",
                    font=small,
                    fill="#d2e0e5",
                )
                if wind is not None:
                    d.text(
                        (12, 82),
                        wind["label"],
                        font=small,
                        fill="#ffd07c" if wind["gust_id"] is not None else "#d2e0e5",
                    )
                if recovery_state is not None:
                    d.text((12, header - 25), recovery_state["label"], font=small, fill="#8feac9")
                target = tmp / f"{i:05d}.png"
                canvas.save(target)
            duration = (
                (frames[i + 1]["image"]["sensor_sim_s"] - stamp) / speed
                if i + 1 < len(frames)
                else 0.25
            )
            concat.extend([f"file '{target}'", f"duration {duration:.9f}"])
        concat.append(f"file '{target}'")
        listing = tmp / "frames.txt"
        listing.write_text("\n".join(concat) + "\n")
        video = output / "battery-flight.mp4"
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
                str(listing),
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
                str(video),
            ],
            check=True,
        )
        shutil.copy2(tmp / f"{len(frames) // 2:05d}.png", output / "battery-preview.png")
    shutil.copy2(Path(__file__), output / "exporter.py")
    available = [a["battery"] for a in aligned if a["battery"]["available"]]
    result = dict(
        schema="missionos.simulated-battery-video.v1",
        run_id=config["run_id"],
        world_sha256=config["world"]["world_sha256"],
        camera=camera,
        playback_speed=speed,
        source="Recorded Gazebo RGB plus PX4 battery_status; time-based synthetic battery",
        model_backend=config.get("decisions", {}).get("backend"),
        telemetry_rule="Latest sample at or before sensor frame, maximum age 2 simulator seconds; no future filling",
        physical_energy_measured=False,
        wind_or_inference_energy_model=False,
        source_trajectory_sha256=sha(run / "flight-trajectory.jsonl"),
        source_frame_manifest_sha256=sha(manifest),
        source_wind_receipts_sha256=sha(wind_path) if wind_path.exists() else None,
        source_recovery_events_sha256=sha(event_path) if recovery is not None else None,
        recovery_display="Event mapped to first telemetry at/after event; never shown before it"
        if recovery is not None
        else None,
        wind_display="Confirmed Gazebo global wind seed; not measured local flow"
        if wind_receipts is not None
        else None,
        source_start_sim_s=frames[0]["image"]["sensor_sim_s"],
        source_end_sim_s=frames[-1]["image"]["sensor_sim_s"],
        frames=len(frames),
        unavailable_frames=len(frames) - len(available),
        remaining_start_fraction=available[0]["remaining_fraction"] if available else None,
        remaining_end_fraction=available[-1]["remaining_fraction"] if available else None,
        video_sha256=sha(video),
        video_bytes=video.stat().st_size,
        exporter_sha256=sha(Path(__file__)),
    )
    (output / "battery-metadata.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "battery-frames.json").write_text(json.dumps(aligned, separators=(",", ":")) + "\n")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--camera", choices=["onboard", "cargo", "queue"], default="onboard")
    p.add_argument("--speed", type=float, default=8)
    args = p.parse_args()
    print(json.dumps(build(args.run, args.output, camera=args.camera, speed=args.speed)))


if __name__ == "__main__":
    main()

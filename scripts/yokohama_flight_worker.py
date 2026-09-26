"""Real PX4 mission and AUTO_LOITER measurements in the isolated city world."""

from __future__ import annotations
import json
import math
import time
from pathlib import Path

ROOT = Path("/mission")
BIN = "/opt/px4-gazebo/bin/px4-"


def hold_metrics(rows, target):
    if len(rows) < 3:
        raise ValueError("Insufficient hold observations")
    for row in rows:
        values = [
            row["sim_s"],
            row["vehicle"]["age_s"],
            *row["vehicle"]["xyz"],
            *row["velocity_ned"],
        ]
        if not all(math.isfinite(v) for v in values) or row["vehicle"]["age_s"] < 0:
            raise ValueError("Non-finite or invalid hold observation")
    if any(b["sim_s"] <= a["sim_s"] for a, b in zip(rows, rows[1:])):
        raise ValueError("Hold observations must have strictly increasing times")
    return {
        "duration_sim_s": rows[-1]["sim_s"] - rows[0]["sim_s"],
        "samples": len(rows),
        "max_horizontal_error_m": max(math.dist(r["vehicle"]["xyz"][:2], target[:2]) for r in rows),
        "max_vertical_error_m": max(abs(r["vehicle"]["xyz"][2] - target[2]) for r in rows),
        "max_px4_speed_mps": max(math.sqrt(sum(v * v for v in r["velocity_ned"])) for r in rows),
        "all_auto_loiter": all(r["nav_state"] == 4 for r in rows),
        "all_airborne_armed": all(r["landed"] is False and r["arming_state"] == 2 for r in rows),
        "max_pose_age_s": max(r["vehicle"]["age_s"] for r in rows),
        "max_sample_gap_sim_s": max(b["sim_s"] - a["sim_s"] for a, b in zip(rows, rows[1:])),
    }


def hold_passes(metrics, config):
    return (
        metrics["duration_sim_s"] >= config["hold_duration_sim_s"]
        and metrics["max_horizontal_error_m"] <= config["hold_horizontal_tolerance_m"]
        and metrics["max_vertical_error_m"] <= config["hold_vertical_tolerance_m"]
        and metrics["max_px4_speed_mps"] <= config["hold_max_speed_mps"]
        and metrics["all_auto_loiter"]
        and metrics["all_airborne_armed"]
        and metrics["max_pose_age_s"] <= 2
        and metrics["max_sample_gap_sim_s"] <= 2
    )


def flight_trial(config, obs, run, field):
    started = time.monotonic()
    phase = "preflight"
    events = (ROOT / "flight-events.jsonl").open("w", buffering=1)
    trajectory = (ROOT / "flight-trajectory.jsonl").open("w", buffering=1)
    holds = []
    frames = []
    video_frames = []
    last_video_sim_s = -10.0

    def event(name, **data):
        row = dict(event=name, phase=phase, wall_s=time.monotonic() - started, **data)
        events.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    def sample():
        nonlocal last_video_sim_s
        raw = {
            key: run([BIN + "listener", key, "-n", "1"], 5)
            for key in [
                "vehicle_local_position",
                "vehicle_status",
                "vehicle_land_detected",
                "mission_result",
            ]
        }
        snap = obs.snapshot()
        v = snap["poses"].get("x500_0")
        if not v or v["age_s"] > 2 or snap["sim_s"] is None:
            raise RuntimeError("Missing or stale independent Gazebo vehicle pose")
        pos, status = raw["vehicle_local_position"], raw["vehicle_status"]
        row = dict(
            run_id=config["run_id"],
            world_sha256=config["world"]["world_sha256"],
            phase=phase,
            sim_s=snap["sim_s"],
            wall_s=time.monotonic() - started,
            vehicle=v,
            local_ned=[field(pos, k) for k in "xyz"],
            velocity_ned=[field(pos, "v" + k) for k in "xyz"],
            position_valid=field(pos, "xy_valid"),
            preflight_pass=field(status, "pre_flight_checks_pass"),
            nav_state=field(status, "nav_state"),
            arming_state=field(status, "arming_state"),
            landed=field(raw["vehicle_land_detected"], "landed"),
            mission_id=field(raw["mission_result"], "mission_id"),
            mission_valid=field(raw["mission_result"], "valid"),
            mission_count=field(raw["mission_result"], "seq_total"),
            raw_px4=raw,
        )
        if any(x is None or not math.isfinite(x) for x in row["local_ned"] + row["velocity_ned"]):
            raise RuntimeError("Invalid PX4 local state")
        trajectory.write(json.dumps(row) + "\n")
        if row["sim_s"] - last_video_sim_s >= 2 and "onboard_rgb" in obs.images:
            captured = obs.images_to_disk(f"flight-{len(video_frames):04d}", ["onboard_rgb"])
            video_frames.append(
                dict(sim_s=row["sim_s"], vehicle=row["vehicle"], image=captured["onboard_rgb"])
            )
            (ROOT / "video-frames.json").write_text(json.dumps(video_frames, indent=2) + "\n")
            last_video_sim_s = row["sim_s"]
        if any(
            "x500" in c["collision1"] + c["collision2"]
            for c in obs.contacts
            if c["topic"] == "city"
        ):
            raise RuntimeError("Vehicle contact with city geometry observed")
        return row

    def wait_for(predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = sample()
            if predicate(row):
                return row
            if time.monotonic() - started > config["timeout_s"] - 10:
                raise TimeoutError("Overall flight timeout")
            time.sleep(0.3)
        raise TimeoutError("Phase timeout: " + phase)

    def upload(name):
        old = field(run([BIN + "listener", "mission_result", "-n", "1"]), "mission_id")
        receipt = json.loads(
            run(["python3", str(ROOT / (name + "-upload.py"))], 40).splitlines()[-1]
        )
        event("upload_receipt", segment=name, receipt=receipt)
        if receipt.get("mission_ack_type") != 0:
            raise RuntimeError("Mission rejected")
        wait_for(
            lambda r: (
                r["mission_valid"] is True
                and r["mission_id"] != old
                and r["mission_count"] == len(receipt["mission_items"])
            ),
            20,
        )

    def activate():
        for _ in range(4):
            run([BIN + "commander", "mode", "auto:mission"])
            try:
                wait_for(lambda r: r["nav_state"] == 3, 3)
                return
            except TimeoutError:
                pass
        raise RuntimeError("AUTO MISSION not observed")

    try:
        discovery_deadline = time.monotonic() + 30
        while time.monotonic() < discovery_deadline:
            snap = obs.snapshot()
            if snap["sim_s"] is not None and "x500_0" in snap["poses"]:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("Gazebo pose discovery timeout")
        for key, value in {
            "COM_RC_IN_MODE": 4,
            "NAV_DLL_ACT": 0,
            "MPC_XY_CRUISE": config["airspeed_mps"],
            "MPC_ACC_HOR": 1,
            "SIM_BAT_DRAIN": 3600,
            "SIM_BAT_MIN_PCT": 0,
            "COM_DISARM_LAND": 2,
            "NAV_ACC_RAD": 0.5,
        }.items():
            run([BIN + "param", "set", key, str(value)])
        wait_for(lambda r: r["position_valid"] is True and r["preflight_pass"] is True, 90)
        event("preflight_observed", observation=sample())
        obs.images_to_disk("scene", ["scene_rgb", "scene_depth"])
        (ROOT / "camera-info.json").write_text(json.dumps(obs.camera_info, indent=2) + "\n")
        for i, stage in enumerate(config["flight_stages"]):
            phase = stage["name"]
            target = stage["target_world_xyz_m"]
            upload(phase)
            if i == 0:
                run([BIN + "commander", "arm"])
                wait_for(lambda r: r["arming_state"] == 2, 10)
            activate()
            wait_for(
                lambda r: (
                    math.dist(r["vehicle"]["xyz"], target) < 0.65
                    and math.sqrt(sum(v * v for v in r["velocity_ned"])) < 0.3
                ),
                150,
            )
            run([BIN + "commander", "mode", "auto:loiter"])
            wait_for(lambda r: r["nav_state"] == 4, 10)
            # Settling is separate from the fixed, uninterrupted measured hold.
            settle = sample()["sim_s"]
            wait_for(lambda r: r["sim_s"] - settle >= 3, 20)
            rows = [sample()]
            event("hold_started", sim_s=rows[0]["sim_s"], target=target)
            while rows[-1]["sim_s"] - rows[0]["sim_s"] < config["hold_duration_sim_s"]:
                time.sleep(0.3)
                rows.append(sample())
            metrics = hold_metrics(rows, target)
            metrics.update(
                start_sim_s=rows[0]["sim_s"],
                end_sim_s=rows[-1]["sim_s"],
                point=phase,
                target_world_xyz_m=target,
                passed=hold_passes(metrics, config),
            )
            holds.append(metrics)
            (ROOT / "hold-results.json").write_text(json.dumps(holds, indent=2) + "\n")
            event("hold_measured", metrics=metrics)
            frames.append(obs.images_to_disk(phase, ["onboard_rgb", "onboard_depth", "down_rgb"]))
            if not metrics["passed"]:
                raise RuntimeError("AP hold did not meet frozen bounds at " + phase)
        phase = "return_land"
        run([BIN + "commander", "land"])
        final = wait_for(
            lambda r: (
                r["landed"] is True
                and r["arming_state"] == 1
                and math.hypot(*r["vehicle"]["xyz"][:2]) < 1.5
            ),
            90,
        )
        event("landing_observed", observation=final)
        pad_contact = any(
            "x500" in c["collision1"] + c["collision2"]
            for c in obs.contacts
            if c["topic"] == "launch_pad"
        )
        if not pad_contact:
            raise RuntimeError("Launch pad contact not observed")
        return dict(
            status="passed",
            holds=holds,
            frames=frames,
            landing_on_launch_pad_observed=True,
            simulation_duration_s=final["sim_s"],
            gazebo_runtime_invoked=True,
            px4_runtime_invoked=True,
            vla_invoked=False,
            wam_invoked=False,
            physical_execution_invoked=False,
            payload_delivery_verified=False,
            native_model_flight=False,
        )
    finally:
        events.close()
        trajectory.close()

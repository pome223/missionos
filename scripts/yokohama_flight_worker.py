"""Real PX4 mission and AUTO_LOITER measurements in the isolated city world."""

from __future__ import annotations
import json
import math
import sys
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
    cargo_frames = []
    last_cargo_sim_s = -10.0
    payload_receipt = None
    decisions = None
    next_connector = None
    landing_xy = (
        config["world"].get("sea_extension", {}).get("ship_deck_world_xyz_m", [0, 0, 0])[:2]
    )

    def event(name, **data):
        row = dict(event=name, phase=phase, wall_s=time.monotonic() - started, **data)
        events.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    def sample():
        nonlocal last_video_sim_s, last_cargo_sim_s
        raw = {
            key: run([BIN + "listener", key, "-n", "1"], 5)
            for key in [
                "vehicle_local_position",
                "vehicle_status",
                "vehicle_land_detected",
                "mission_result",
                "battery_status",
            ]
        }
        if config["world"].get("sea_extension"):
            for key in ("vehicle_global_position", "home_position"):
                raw[key] = run([BIN + "listener", key, "-n", "1"], 5)
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
            heading_ned_rad=field(pos, "heading"),
            reset_counters=[
                field(pos, key)
                for key in ("xy_reset_counter", "z_reset_counter", "heading_reset_counter")
            ],
            battery_fraction=field(raw["battery_status"], "remaining"),
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
        if config["world"].get("sea_extension"):
            global_alt = field(raw["vehicle_global_position"], "alt")
            home_alt = field(raw["home_position"], "alt")
            row["px4_relative_altitude_m"] = (
                global_alt - home_alt
                if global_alt is not None
                and home_alt is not None
                and field(raw["home_position"], "valid_alt") is True
                else None
            )
        if config["world"].get("payload_delivery"):
            row["payload"] = snap["poses"].get("delivery_payload")
            row["payload_joint"] = snap["payload_joint"]
        if config["world"].get("wind"):
            row["wind_probe"] = {k: snap["poses"].get(k) for k in ("wind_witness", "wind_control")}
        trajectory.write(json.dumps(row) + "\n")
        if (
            config["world"].get("payload_delivery")
            and phase.startswith("PAYLOAD-")
            and row["sim_s"] - last_cargo_sim_s >= 0.24
            and "delivery_rgb" in obs.images
        ):
            captured = obs.images_to_disk(f"cargo-{len(cargo_frames):04d}", ["delivery_rgb"])
            cargo_frames.append(
                dict(
                    sim_s=row["sim_s"],
                    phase=phase,
                    payload=row["payload"],
                    vehicle=row["vehicle"],
                    image=captured["delivery_rgb"],
                )
            )
            (ROOT / "payload-video-frames.json").write_text(
                json.dumps(cargo_frames, indent=2) + "\n"
            )
            last_cargo_sim_s = row["sim_s"]
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
        obs.record_motion(row)
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
        if decisions and name.startswith("city-"):
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run, ["python3", str(ROOT / (name + "-upload.py"))], 40)
                while not future.done():
                    sample()
                    time.sleep(0.1)
                uploaded = future.result()
        else:
            uploaded = run(["python3", str(ROOT / (name + "-upload.py"))], 40)
        receipt = json.loads(uploaded.splitlines()[-1])
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

    def activate(*, expires_at_wall_s=None):
        for _ in range(4):
            if expires_at_wall_s is not None and time.monotonic() - started > expires_at_wall_s:
                raise ValueError("City activation expired before mode command")
            run([BIN + "commander", "mode", "auto:mission"])
            try:
                wait_for(lambda r: r["nav_state"] == 3, 3)
                return
            except TimeoutError:
                pass
        raise RuntimeError("AUTO MISSION not observed")

    try:
        if config.get("decisions"):
            from yokohama_decision_worker import CityDecisions, physical_heading

            decisions = CityDecisions(
                ROOT, config, sample, event, lambda: time.monotonic() - started
            )
        discovery_deadline = time.monotonic() + 30
        while time.monotonic() < discovery_deadline:
            snap = obs.snapshot()
            if snap["sim_s"] is not None and "x500_0" in snap["poses"]:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("Gazebo pose discovery timeout")
        if "simulator_initial_heading_deg" in config and not decisions:
            from yokohama_decision_worker import physical_heading
        if "simulator_heading_runtime" in config:
            expected = config["simulator_heading_runtime"]
            px4_version = run([BIN + "ver", "all"])
            gz_version = run(["dpkg-query", "-W", "-f=${Version}", "libgz-sim8"])
            if (
                expected["px4_git"] not in px4_version
                or gz_version.strip() != expected["gz_sim_version"]
            ):
                raise ValueError("Unverified simulator compass implementation")
            event("simulator_heading_runtime_verified", px4=px4_version, gz=gz_version)
        for key, value in {
            "COM_RC_IN_MODE": 4,
            "NAV_DLL_ACT": 0,
            "MPC_XY_CRUISE": config["airspeed_mps"],
            "MPC_ACC_HOR": 1,
            "SIM_BAT_DRAIN": 3600,
            "SIM_BAT_MIN_PCT": 0,
            "COM_DISARM_LAND": 2,
            "NAV_ACC_RAD": 0.5,
            **({"EKF2_MAG_TYPE": 6} if "simulator_initial_heading_deg" in config else {}),
        }.items():
            run([BIN + "param", "set", key, str(value)])
        event(
            "battery_simulation_parameters",
            drain=run([BIN + "param", "show", "SIM_BAT_DRAIN"]),
            floor=run([BIN + "param", "show", "SIM_BAT_MIN_PCT"]),
            source="PX4 battery_simulator; time-based, no current sensor",
        )
        wait_for(lambda r: r["position_valid"] is True and r["preflight_pass"] is True, 90)
        event("preflight_observed", observation=sample())
        if "simulator_initial_heading_deg" in config:
            initial_heading = config["simulator_initial_heading_deg"]
            before = sample()
            if (
                before["landed"] is not True
                or before["arming_state"] != 1
                or abs(
                    math.remainder(
                        physical_heading(before) - math.radians(initial_heading), 2 * math.pi
                    )
                )
                > 0.03
            ):
                raise ValueError("Authored simulator launch heading not observed before arming")
            run([BIN + "commander", "set_heading", str(initial_heading)])
            event(
                "simulator_initial_heading_command",
                heading_deg=initial_heading,
                source=config["simulator_heading_runtime"]["source"],
                observation=before,
            )
            aligned = wait_for(
                lambda r: (
                    abs(math.remainder(r["heading_ned_rad"] - physical_heading(r), 2 * math.pi))
                    <= 0.03
                ),
                45,
            )
            event(
                "simulator_initial_heading_observed",
                observation=aligned,
                parameters={"EKF2_MAG_TYPE": run([BIN + "param", "show", "EKF2_MAG_TYPE"])},
            )
        obs.images_to_disk("scene", ["scene_rgb", "scene_depth"])
        (ROOT / "camera-info.json").write_text(json.dumps(obs.camera_info, indent=2) + "\n")
        for i, stage in enumerate(config["flight_stages"]):
            phase = stage["name"]
            target = stage["target_world_xyz_m"]
            if phase == "PAYLOAD-CLIMB":
                if payload_receipt is None:
                    raise RuntimeError("No observed cargo receipt; return remains unauthorized")
                event("payload_return_authorized", receipt_id=payload_receipt["receipt_id"])
            run(
                [
                    BIN + "param",
                    "set",
                    "MPC_XY_CRUISE",
                    str(stage.get("airspeed_mps", config["airspeed_mps"])),
                ]
            )
            if phase.startswith("SEA-"):
                if decisions and (
                    decisions.active
                    or (
                        i > 0
                        and phase in {"SEA-OUTBOUND-COAST", "SEA-RETURN"}
                        and not decisions.closed
                    )
                ):
                    raise RuntimeError("Model session active on an AP-only sea leg")
                event(
                    "sea_ap_only_observed",
                    observation=sample(),
                    models_active=bool(decisions and decisions.active),
                    session_closed=bool(decisions and decisions.closed),
                )
            upload(next_connector or phase)
            next_connector = None
            if i == 0:
                run([BIN + "commander", "arm"])
                wait_for(lambda r: r["arming_state"] == 2, 10)
            activate()
            wait_for(
                lambda r: (
                    math.dist(r["vehicle"]["xyz"], target) < 0.65
                    and math.sqrt(sum(v * v for v in r["velocity_ned"])) < 0.3
                ),
                stage.get("arrival_timeout_s", 150),
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
            if config["world"].get("payload_delivery") and phase == "SEA-TAKEOFF":
                from yokohama_payload import fresh_pose

                carried = sample()
                if (
                    not fresh_pose(carried, "payload")
                    or math.dist(carried["vehicle"]["xyz"], carried["payload"]["xyz"]) > 0.8
                ):
                    raise RuntimeError("Cargo did not take off attached to the vehicle")
                event("payload_airborne_observed", observation=carried)
            if phase == "SEA-TAKEOFF" and config["world"].get("wind", {}).get("after_takeoff"):
                activation = obs.activate_wind(config["world"]["wind"]["velocity_enu_mps"])
                activation.update(run_id=config["run_id"], phase=phase)
                (ROOT / "wind-activation.json").write_text(json.dumps(activation, indent=2) + "\n")
                event("wind_activated", activation=activation)
                if not activation["confirmed"]:
                    raise RuntimeError("Wind activation was not confirmed by Gazebo")
                wait_for(lambda r: r["sim_s"] - activation["end_sim_s"] >= 10, 30)
            if phase == "PAYLOAD-LOW":
                from yokohama_payload import atomic_json, digest, require_release, require_receipt

                if decisions and (decisions.active or not decisions.closed):
                    raise RuntimeError("City models must be stopped before cargo release")
                request = require_release(config, sample())
                atomic_json(ROOT / "payload-request.json", request)
                event("payload_release_requested", request_sha256=digest(request), request=request)

                def detach(attempt):
                    run(
                        [
                            "gz",
                            "topic",
                            "-t",
                            config["world"]["payload_delivery"]["detach_topic"],
                            "-m",
                            "gz.msgs.Empty",
                            "-p",
                            "",
                        ]
                    )
                    event("payload_detach_command", attempt=attempt)

                detach(1)
                phase = "PAYLOAD-VERIFY"
                attempts, retry_at = 1, time.monotonic() + 5
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    observed = sample()
                    receipt_path = ROOT / "payload-receipt.json"
                    if receipt_path.exists():
                        payload_receipt = json.loads(receipt_path.read_text())
                        require_receipt(config, request, payload_receipt, observed)
                        event(
                            "payload_received",
                            receipt=payload_receipt,
                            receipt_sha256=digest(payload_receipt),
                            observation=observed,
                        )
                        obs.images_to_disk("payload-received", ["delivery_rgb", "down_rgb"])
                        break
                    if (
                        attempts < 3
                        and time.monotonic() >= retry_at
                        and observed.get("payload")
                        and (observed.get("payload_joint") or {}).get("state") != "detached"
                        and math.dist(observed["payload"]["xyz"], observed["vehicle"]["xyz"]) < 0.8
                    ):
                        attempts += 1
                        detach(attempts)
                        retry_at = time.monotonic() + 5
                    time.sleep(0.3)
                else:
                    raise TimeoutError("No verified pad receipt; return remains unauthorized")
            if decisions and phase in ("00-D1", "01-D2"):
                permit = decisions.decide(obs, config["flight_stages"][i + 1]["target_world_xyz_m"])
                candidate = permit["candidate"]
                upload(permit["upload_name"])
                permit = decisions.activation_permit(permit)
                # Mission upload does not itself move the vehicle. Recheck hold
                # and the bound two-second issuance age before activation.
                if time.monotonic() - started > permit["expires_at_worker_wall_s"]:
                    raise ValueError("City permit expired before mission activation")
                activate(expires_at_wall_s=permit["expires_at_worker_wall_s"])
                event("city_segment_dispatched", permit_id=permit["permit_id"])
                target_model = candidate["target_world_xyz_m"]

                def at_model_target(r):
                    begin = permit["rules"]["start_world_xyz_m"]
                    delta = [b - a for a, b in zip(begin, target_model)]
                    length2 = sum(x * x for x in delta)
                    fraction = max(
                        0.0,
                        min(
                            1.0,
                            sum((p - a) * d for p, a, d in zip(r["vehicle"]["xyz"], begin, delta))
                            / length2,
                        ),
                    )
                    nearest = [a + fraction * d for a, d in zip(begin, delta)]
                    if math.dist(r["vehicle"]["xyz"], nearest) > 1:
                        raise ValueError("City segment left the 1 m tracking tube")
                    return (
                        math.dist(r["vehicle"]["xyz"], target_model) <= 0.25
                        and abs(r["vehicle"]["xyz"][2] - target_model[2]) <= 0.15
                        and math.hypot(*r["velocity_ned"]) <= 0.3
                        and abs(
                            math.remainder(
                                r["heading_ned_rad"]
                                - permit.get(
                                    "executor_heading_ned_rad", candidate["target_heading_ned_rad"]
                                ),
                                2 * math.pi,
                            )
                        )
                        <= 0.05
                        and abs(
                            math.remainder(
                                physical_heading(r) - candidate["target_heading_world_ned_rad"],
                                2 * math.pi,
                            )
                        )
                        <= 0.05
                    )

                arrived = wait_for(at_model_target, 45)
                run([BIN + "commander", "mode", "auto:loiter"])
                stable = None

                def settled(r):
                    nonlocal stable
                    okay = at_model_target(r) and r["nav_state"] == 4
                    stable = (r["sim_s"] if stable is None else stable) if okay else None
                    return stable is not None and r["sim_s"] - stable >= 2

                arrived = wait_for(settled, 20)
                decisions.completed.append(dict(permit=permit, arrived=arrived))
                event("city_segment_arrived", permit_id=permit["permit_id"], observation=arrived)
                decisions.record_arrival(obs, permit, arrived)
                import hashlib

                connector = ROOT / (permit["connector_name"] + "-upload.py")
                if hashlib.sha256(connector.read_bytes()).hexdigest() != permit["connector_sha256"]:
                    raise ValueError("AP connector differs from independently checked route")
                next_connector = permit["connector_name"]
                if phase == "01-D2":
                    decisions.stop()
        phase = "return_land"
        run([BIN + "commander", "land"])
        final = wait_for(
            lambda r: (
                r["landed"] is True
                and r["arming_state"] == 1
                and math.dist(r["vehicle"]["xyz"][:2], landing_xy) < 1.5
            ),
            90,
        )
        event("landing_observed", observation=final)
        pad_contact = any(
            "x500" in c["collision1"] + c["collision2"]
            for c in obs.contacts
            if c["topic"] == "launch_pad" and c["sensor_sim_s"] >= final["sim_s"] - 10
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
            vla_invoked=bool(decisions and config["decisions"]["backend"] == "native"),
            wam_invoked=bool(decisions and config["decisions"]["backend"] == "native"),
            physical_execution_invoked=False,
            payload_delivery_verified=bool(payload_receipt),
            payload_receipt_id=payload_receipt["receipt_id"] if payload_receipt else None,
            native_model_flight=bool(
                decisions
                and len(decisions.completed) == 2
                and config["decisions"]["backend"] == "native"
            ),
            city_decision_updates=len(decisions.completed) if decisions else 0,
            city_decision_backend=config.get("decisions", {}).get("backend"),
        )
    finally:
        primary_failure = sys.exc_info()[0] is not None
        try:
            if decisions:
                try:
                    decisions.stop()
                except Exception as cleanup_error:
                    event(
                        "city_shutdown_failed",
                        reason=type(cleanup_error).__name__ + ": " + str(cleanup_error),
                    )
                    if not primary_failure:
                        raise
        finally:
            events.close()
            trajectory.close()

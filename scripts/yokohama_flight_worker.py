"""Real PX4 mission and AUTO_LOITER measurements in the isolated city world."""

from __future__ import annotations
import json
import math
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path("/mission")
BIN = "/opt/px4-gazebo/bin/px4-"

if TYPE_CHECKING or __package__:
    from .yokohama_endpoint_feedback import (
        feedback_policy,
        goal_reached,
        validate_feedback_candidate,
        validate_feedback_observation,
    )
    from .yokohama_decision_worker import physical_heading
    from .yokohama_altitude_contract import (
        SCHEMA,
        UPLOAD_PREPARATION_SCHEMA,
        validate_upload_preparation,
        mission_wire_items,
        AltitudeReferenceChanged,
        AltitudeOffsetChanged,
        compile_mission,
        digest as altitude_digest,
        recheck_mapping,
    )
else:
    from yokohama_endpoint_feedback import (
        feedback_policy,
        goal_reached,
        validate_feedback_candidate,
        validate_feedback_observation,
    )
    from yokohama_decision_worker import physical_heading
    from yokohama_altitude_contract import (
        SCHEMA,
        UPLOAD_PREPARATION_SCHEMA,
        validate_upload_preparation,
        mission_wire_items,
        AltitudeReferenceChanged,
        AltitudeOffsetChanged,
        compile_mission,
        digest as altitude_digest,
        recheck_mapping,
    )


def prepare_altitude_upload(
    root,
    config,
    name,
    observation,
    now_worker_wall_s,
    *,
    expected_world_items_sha256=None,
):
    """Compile only the transport representation of an immutable world template."""
    import hashlib

    stage = next((s for s in config["flight_stages"] if s["name"] == name), None)
    if stage is not None:
        items = stage["items"]
    else:
        raw = (root / (name + "-world-items.json")).read_bytes()
        if (
            not expected_world_items_sha256
            or hashlib.sha256(raw).hexdigest() != expected_world_items_sha256
        ):
            raise ValueError("Connector world template differs from its permit")
        items = json.loads(raw)
    mapping = compile_mission(
        items,
        observation,
        segment=name,
        run_id=config["run_id"],
        world_sha256=config["world"]["world_sha256"],
        now_worker_wall_s=now_worker_wall_s,
    )
    (root / (name + "-altitude-items.json")).write_text(
        json.dumps(mapping["mission_items"], allow_nan=False) + "\n"
    )
    (root / (name + "-altitude-mapping.json")).write_text(
        json.dumps(mapping, allow_nan=False) + "\n"
    )
    return mapping


def send_mapped_mission_command(
    run,
    event,
    mapping,
    current,
    clock,
    *,
    expires_at_wall_s=None,
    pad_permission=None,
    pad_check=None,
):
    """Final deadline barrier after observation/validation, immediately before send."""
    recheck_mapping(mapping, current, clock())
    identity = altitude_digest(mapping)
    if pad_check is not None:
        pad_check()
    command_wall_s = clock()
    if not 0 <= command_wall_s - current["altitude_capture"]["end_worker_wall_s"] <= 2:
        raise ValueError("Altitude observation expired before mode command")
    if expires_at_wall_s is not None and (
        not math.isfinite(expires_at_wall_s) or command_wall_s > expires_at_wall_s
    ):
        raise ValueError("City activation expired before mode command")
    if pad_permission is not None:
        age = command_wall_s - pad_permission["rules_checked_at"]["wall_s"]
        if not math.isfinite(age) or not 0 <= age <= 30:
            raise ValueError("Missing or expired pad entry permission")
    run([BIN + "commander", "mode", "auto:mission"])
    event(
        "altitude_transport_command_sent",
        segment=mapping["segment"],
        mapping_sha256=identity,
        observation=current,
        dispatched_at_worker_wall_s=command_wall_s,
    )


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


def execute_model_segments(
    config,
    decisions,
    obs,
    next_target,
    upload,
    activate,
    wait_for,
    run,
    event,
    clock,
    *,
    prepare_upload=None,
):
    """Execute the approved model segment(s), recording each observed endpoint.

    The callbacks are the flight worker's existing PX4/observation boundary.
    Endpoint feedback keeps one goal and has no AP connector between cycles.
    """
    feedback = feedback_policy(config)
    if feedback and decisions.completed:
        raise ValueError("Endpoint feedback session has already executed")
    model_goal = list(feedback["goal_world_xyz_m"] if feedback else next_target)
    deadline = clock() + feedback["total_timeout_s"] if feedback else None
    if feedback:
        decisions.endpoint_deadline = deadline

    def check_deadline():
        if deadline is not None and clock() >= deadline:
            raise TimeoutError("Endpoint feedback total deadline expired")

    def remaining(timeout):
        check_deadline()
        return min(timeout, max(0.0, deadline - clock())) if deadline is not None else timeout

    for _ in range(feedback["max_cycles"] if feedback else 1):
        check_deadline()
        permit = decisions.prepare_segment(
            obs,
            model_goal,
            upload,
            prepare_upload=prepare_upload,
        )
        candidate = permit["candidate"]
        target_model = candidate["target_world_xyz_m"]
        # Mission upload does not itself move the vehicle. Recheck hold
        # and the bound two-second issuance age before activation.
        check_deadline()
        if feedback:
            validate_feedback_candidate(
                config,
                permit["rules"].get("start_world_xyz_m"),
                target_model,
                origin=permit["rules"].get("proposal_origin_world_xyz_m"),
            )
        if clock() > permit["expires_at_worker_wall_s"]:
            raise ValueError("City permit expired before mission activation")
        check_deadline()
        activation_expires = permit["expires_at_worker_wall_s"]
        if deadline is not None:
            activation_expires = min(activation_expires, deadline)
        activate(expires_at_wall_s=activation_expires)
        check_deadline()
        event("city_segment_dispatched", permit_id=permit["permit_id"])

        def at_model_target(r):
            check_deadline()
            if feedback:
                validate_feedback_observation(config, r)
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

        arrived = wait_for(at_model_target, remaining(45))
        check_deadline()
        run([BIN + "commander", "mode", "auto:loiter"])
        check_deadline()
        stable = None
        stable_samples = 0
        previous_settle_sim_s = None

        def settled(r):
            nonlocal stable, stable_samples, previous_settle_sim_s
            okay = at_model_target(r) and r["nav_state"] == 4
            if feedback and okay:
                validate_feedback_observation(config, r, held=True)
            if (
                feedback
                and previous_settle_sim_s is not None
                and r["sim_s"] < previous_settle_sim_s
            ):
                raise ValueError("Endpoint settling simulation clock regressed")
            distinct_sample = previous_settle_sim_s is None or r["sim_s"] > previous_settle_sim_s
            previous_settle_sim_s = r["sim_s"]
            if not okay:
                stable, stable_samples = None, 0
            elif stable is None:
                stable, stable_samples = r["sim_s"], 1
            elif not feedback or distinct_sample:
                stable_samples += 1
            return (
                stable is not None
                and r["sim_s"] - stable >= 2
                and (not feedback or stable_samples >= 3)
            )

        arrived = wait_for(settled, remaining(20))
        check_deadline()
        completed = dict(permit=permit, arrived=arrived)
        if feedback:
            completed.update(stable_since_sim_s=stable, stable_samples=stable_samples)
        decisions.completed.append(completed)
        event("city_segment_arrived", permit_id=permit["permit_id"], observation=arrived)
        decisions.record_arrival(obs, permit, arrived)
        check_deadline()
        if feedback and goal_reached(config, arrived):
            break
    if feedback:
        decisions.stop()
        check_deadline()
        if not decisions.closed or decisions.active:
            raise ValueError("Endpoint feedback session remains active before AP exit")
        event(
            "city_endpoint_feedback_completed",
            completed_updates=len(decisions.completed),
            model_goal_reached=goal_reached(config, arrived),
            goal_world_xyz_m=model_goal,
            exit_world_xyz_m=feedback["exit_world_xyz_m"],
        )
        if not permit.get("connector_name") or not permit.get("connector_sha256"):
            raise ValueError("Endpoint feedback has no approved final AP exit")
    return permit


def flight_trial(config, obs, run, field):
    endpoint_feedback = feedback_policy(config)
    if config.get("altitude_transport_contract") != SCHEMA:
        raise ValueError("Unified altitude transport contract required before flight")
    started = time.monotonic()
    phase = "preflight"
    altitude_collector = None
    events = (ROOT / "flight-events.jsonl").open("w", buffering=1)
    trajectory = (ROOT / "flight-trajectory.jsonl").open("w", buffering=1)
    holds = []
    frames = []
    video_frames = []
    last_video_sim_s = -10.0
    cargo_frames = []
    last_cargo_sim_s = -10.0
    payload_receipt = None
    pad_queue = None
    queue_frames = []
    last_queue_sim_s = -10.0
    wind_profile = config["world"].get("wind", {}).get("profile")
    wind_zone = None
    wind_state_applied = None
    gust_epoch_sim_s = None
    gust_last_check_sim_s = None
    wind_transitions = []
    wind_profile_active = False
    last_diagnostic_sim_s = -10.0
    decisions = None
    model_phases = []
    next_connector = None
    active_altitude_mapping = None
    connector_world_hashes = {}
    recovery_state = {}
    landing_xy = (
        config["world"].get("sea_extension", {}).get("ship_deck_world_xyz_m", [0, 0, 0])[:2]
    )

    def event(name, **data):
        if name == "candidate_rejected":
            recovery_state.update(anchor=data["observation"],
                                  deadline=data["recovery_deadline_wall_s"])
        row = dict(event=name, phase=phase, wall_s=time.monotonic() - started, **data)
        events.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    def update_profile_wind():
        nonlocal wind_zone, wind_state_applied, gust_epoch_sim_s, gust_last_check_sim_s
        from yokohama_wind_profile import wind_state, check_gust_deadlines

        snap = obs.snapshot()
        pose = snap["poses"].get("x500_0")
        if gust_epoch_sim_s is None:
            gust_epoch_sim_s = snap["sim_s"]
            gust_last_check_sim_s = snap["sim_s"]
        check_gust_deadlines(wind_profile, gust_epoch_sim_s, gust_last_check_sim_s, snap["sim_s"])
        state = wind_state(wind_profile, pose, snap["sim_s"], wind_zone, gust_epoch_sim_s)
        zone, velocity = state["zone"], state["requested_enu_mps"]
        if state == wind_state_applied:
            gust_last_check_sim_s = snap["sim_s"]
            return
        receipt = obs.activate_wind(velocity)
        receipt.update(
            run_id=config["run_id"],
            world_sha256=config["world"]["world_sha256"],
            phase=phase,
            sequence=len(wind_transitions),
            previous_zone=wind_zone,
            zone=zone,
            observation_sim_s=snap["sim_s"],
            vehicle=pose,
        )
        receipt.update(state)
        wind_transitions.append(receipt)
        (ROOT / "wind-transitions.json").write_text(json.dumps(wind_transitions, indent=2) + "\n")
        event("wind_state_requested", receipt=receipt)
        if not receipt["confirmed"]:
            raise RuntimeError("Wind zone change was not confirmed by Gazebo")
        check_gust_deadlines(
            wind_profile, gust_epoch_sim_s, gust_last_check_sim_s, receipt["end_sim_s"]
        )
        gust_last_check_sim_s = receipt["end_sim_s"]
        wind_zone = zone
        wind_state_applied = state
        if len(wind_transitions) == 1:
            (ROOT / "wind-activation.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def sample():
        nonlocal last_video_sim_s, last_cargo_sim_s, last_diagnostic_sim_s, last_queue_sim_s
        if recovery_state and time.monotonic() - started >= recovery_state["deadline"]:
            raise TimeoutError("Recovery observation deadline exceeded")
        if pad_queue:
            pad_queue.update_actor()
        if wind_profile_active:
            update_profile_wind()
        capture_begin = time.monotonic() - started
        capture_before = obs.snapshot()["sim_s"]
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
        if config.get("altitude_transport_contract") == SCHEMA or config["world"].get(
            "sea_extension"
        ):
            for key in ("vehicle_global_position", "home_position"):
                raw[key] = run([BIN + "listener", key, "-n", "1"], 0.2)
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
        if config.get("altitude_transport_contract") == SCHEMA or config["world"].get(
            "sea_extension"
        ):
            global_alt = field(raw["vehicle_global_position"], "alt")
            home_alt = field(raw["home_position"], "alt")
            row["px4_relative_altitude_m"] = (
                global_alt - home_alt
                if global_alt is not None
                and home_alt is not None
                and field(raw["home_position"], "valid_alt") is True
                else None
            )
        if config.get("altitude_transport_contract") == SCHEMA:
            row["altitude_capture"] = dict(
                begin_worker_wall_s=capture_begin,
                end_worker_wall_s=row["wall_s"],
                sim_before_s=capture_before,
            )
        if config["world"].get("payload_delivery"):
            row["payload"] = snap["poses"].get("delivery_payload")
            row["payload_joint"] = snap["payload_joint"]
        if pad_queue:
            row["queue_lead"] = snap["poses"].get("queue_lead")
            row["queue_parcel"] = snap["poses"].get("queue_parcel")
        if config["world"].get("wind"):
            row["wind_probe"] = {k: snap["poses"].get(k) for k in ("wind_witness", "wind_control")}
            if wind_profile:
                row["wind_zone"] = wind_zone
                row["wind_transition_sequence"] = len(wind_transitions) - 1
                row["wind_gust_id"] = (
                    wind_state_applied.get("gust_id") if wind_state_applied else None
                )
                row["wind_requested_enu_mps"] = (
                    wind_state_applied.get("requested_enu_mps") if wind_state_applied else None
                )
            if row["arming_state"] == 2 and row["sim_s"] - last_diagnostic_sim_s >= 5:
                diagnostic = dict(
                    run_id=config["run_id"],
                    sim_s=row["sim_s"],
                    phase=phase,
                    raw_px4={
                        key: run([BIN + "listener", key, "-n", "1"], 5)
                        for key in (
                            "trajectory_setpoint",
                            "vehicle_local_position_setpoint",
                            "position_setpoint_triplet",
                            "vehicle_attitude_setpoint",
                            "actuator_motors",
                        )
                    },
                )
                with (ROOT / "wind-diagnostics.jsonl").open("a") as diagnostic_file:
                    diagnostic_file.write(json.dumps(diagnostic) + "\n")
                last_diagnostic_sim_s = row["sim_s"]
        if recovery_state:
            if __package__:
                from .yokohama_candidate_recovery import validate_sample
            else:
                from yokohama_candidate_recovery import validate_sample
            if time.monotonic() - started >= recovery_state["deadline"]:
                raise TimeoutError("Recovery observation deadline exceeded")
            validate_sample(config, row, recovery_state["anchor"], landing=phase == "recovery_land")
        trajectory.write(json.dumps(row) + "\n")
        if pad_queue:
            pad_queue.guard(row)
            if (
                phase in ("02-D3", "03-DELIVERY", "PAYLOAD-LOW", "PAYLOAD-VERIFY", "PAYLOAD-CLIMB")
                and row["sim_s"] - last_queue_sim_s >= 0.24
                and "queue_rgb" in obs.images
            ):
                capture = obs.images_to_disk(f"queue-{len(queue_frames):04d}", ["queue_rgb"])
                queue_frames.append(
                    dict(sim_s=row["sim_s"], phase=phase, image=capture["queue_rgb"])
                )
                (ROOT / "queue-video-frames.json").write_text(
                    json.dumps(queue_frames, indent=2) + "\n"
                )
                last_queue_sim_s = row["sim_s"]
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

    prepared_protocols = {}

    def prepare_upload_protocol(name):
        if name in prepared_protocols:
            raise ValueError("Upload preparation already outstanding")
        from concurrent.futures import ThreadPoolExecutor
        from uuid import uuid4

        old = field(run([BIN + "listener", "mission_result", "-n", "1"]), "mission_id")
        transaction = dict(
            transaction_id=uuid4().hex,
            run_id=config["run_id"],
            world_sha256=config["world"]["world_sha256"],
            segment=name,
            worker_epoch_monotonic_s=started,
            reuse_mavlink_session=name != config["flight_stages"][0]["name"],
        )
        (ROOT / "upload-transaction.json").write_text(json.dumps(transaction, allow_nan=False))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, ["python3", str(ROOT / "upload-prepare.py")], 40)
            while not future.done():
                sample()
                time.sleep(0.1)
            preparation = json.loads(future.result().splitlines()[-1])
        validate_upload_preparation(transaction, preparation)
        event(
            "upload_protocol_prepared",
            segment=name,
            transaction=transaction,
            preparation=preparation,
        )
        prepared_protocols[name] = (transaction, preparation, old)
        return preparation

    def upload(name):
        nonlocal active_altitude_mapping
        active_altitude_mapping = None
        transaction = preparation = None
        if config.get("mission_upload_preparation") == UPLOAD_PREPARATION_SCHEMA:
            if name not in prepared_protocols:
                prepare_upload_protocol(name)
            transaction, preparation, old = prepared_protocols.pop(name)
        else:
            old = field(run([BIN + "listener", "mission_result", "-n", "1"]), "mission_id")
        if config.get("altitude_transport_contract") == SCHEMA:
            if (
                name in {s["name"] for s in config["flight_stages"]}
                or name in connector_world_hashes
            ):
                active_altitude_mapping = prepare_altitude_upload(
                    ROOT,
                    config,
                    name,
                    sample(),
                    time.monotonic() - started,
                    expected_world_items_sha256=connector_world_hashes.get(name),
                )
            else:
                active_altitude_mapping = json.loads(
                    (ROOT / (name + "-altitude-mapping.json")).read_text()
                )
        if transaction is not None:
            if (
                active_altitude_mapping["observation"]["altitude_capture"]["begin_worker_wall_s"]
                < preparation["prepared_at_worker_wall_s"]
            ):
                raise ValueError("Altitude mapping predates session/clear preparation")
            (ROOT / (name + "-upload-binding.json")).write_text(
                json.dumps(
                    dict(
                        transaction=transaction,
                        preparation=preparation,
                        mapping=active_altitude_mapping,
                        mapping_sha256=altitude_digest(active_altitude_mapping),
                    ),
                    allow_nan=False,
                )
            )
        send_argv = ["python3", str(ROOT / (name + "-upload.py"))]
        if transaction is not None:
            send_argv.append(transaction["transaction_id"])
        if decisions and (name.startswith("city-") or config.get("candidate_recovery")):
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run, send_argv, 40)
                while not future.done():
                    sample()
                    time.sleep(0.1)
                uploaded = future.result()
        else:
            uploaded = run(send_argv, 40)
        receipt = json.loads(uploaded.splitlines()[-1])
        if transaction is not None and (
            receipt.get("transaction_id") != transaction["transaction_id"]
            or receipt.get("transaction_sha256") != altitude_digest(transaction)
            or receipt.get("mapping_sha256") != altitude_digest(active_altitude_mapping)
            or receipt.get("preparation") != preparation
            or receipt.get("mission_items_wire")
            != mission_wire_items(active_altitude_mapping["mission_items"])
        ):
            raise ValueError("Upload receipt belongs to a different preparation")
        if active_altitude_mapping is not None:
            if receipt["mission_items"] != active_altitude_mapping["mission_items"]:
                raise ValueError("Uploaded altitude differs from mapped mission")
            event(
                "altitude_transport_prepared",
                segment=name,
                transaction_id=transaction["transaction_id"] if transaction else None,
                mapping=active_altitude_mapping,
                mapping_sha256=altitude_digest(active_altitude_mapping),
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

    def activate(*, expires_at_wall_s=None):
        for _ in range(4):
            if expires_at_wall_s is not None and time.monotonic() - started > expires_at_wall_s:
                raise ValueError("City activation expired before mode command")
            current = sample()
            pad_check_required = pad_queue and (
                phase == "03-DELIVERY" or (phase == "02-D3" and pad_queue.entered)
            )
            if active_altitude_mapping is None:
                raise ValueError("Missing altitude transport before activation")
            send_mapped_mission_command(
                run,
                event,
                active_altitude_mapping,
                current,
                lambda: time.monotonic() - started,
                expires_at_wall_s=expires_at_wall_s,
                pad_permission=pad_queue.permission if pad_check_required else None,
                pad_check=(lambda: pad_queue.require_dispatch(lambda: current))
                if pad_check_required
                else None,
            )
            try:
                wait_for(lambda r: r["nav_state"] == 3, 3)
                return
            except TimeoutError:
                pass
        raise RuntimeError("AUTO MISSION not observed")

    try:
        if config.get("altitude_diagnostics"):
            from yokohama_altitude import AltitudeCollector

            altitude_collector = AltitudeCollector(
                obs, config, lambda: phase, ROOT / "altitude-diagnostics.jsonl"
            )
            altitude_collector.start()
        if config["world"].get("pad_queue"):
            from yokohama_pad_worker import PadQueue

            pad_queue = PadQueue(ROOT, config, obs, event)
        if config.get("decisions"):
            from yokohama_decision_worker import CityDecisions, decision_phases, physical_heading

            decisions = CityDecisions(
                ROOT, config, sample, event, lambda: time.monotonic() - started
            )
            model_phases = list(decision_phases(config).values())
            if pad_queue and config["world"]["pad_queue"].get("fault_lead_return"):
                decisions.on_request = pad_queue.trigger_fault
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
        if config["world"].get("wind"):
            event(
                "wind_ap_parameters",
                values={
                    k: run([BIN + "param", "show", k])
                    for k in (
                        "MPC_THR_MAX",
                        "MPC_THR_HOVER",
                        "MPC_TILTMAX_AIR",
                        "MPC_XY_VEL_MAX",
                        "MPC_ACC_HOR",
                        "MPC_JERK_AUTO",
                        "MPC_YAW_MODE",
                    )
                },
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
            if pad_queue and phase == "03-DELIVERY" and pad_queue.hold is not None:
                # The model endpoint is a new hold; entry needs fresh clear evidence.
                pad_queue.reconfirm(sample, "model_endpoint_before_delivery_connector")
            upload(next_connector or phase)
            next_connector = None
            if i == 0:
                run([BIN + "commander", "arm"])
                wait_for(lambda r: r["arming_state"] == 2, 10)
                if active_altitude_mapping is not None:
                    current = sample()
                    try:
                        recheck_mapping(
                            active_altitude_mapping, current, time.monotonic() - started
                        )
                    except (AltitudeReferenceChanged, AltitudeOffsetChanged):
                        if current["landed"] is not True:
                            raise
                        # Same approved world mission; bind the final armed home
                        # reference. No auto mode command has been sent yet.
                        event("altitude_home_rebind_before_takeoff", observation=current)
                        upload(phase)
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
            if pad_queue and phase == "02-D3":
                pad_queue.await_clearance(sample)
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
                if wind_profile:
                    wind_profile_active = True
                    update_profile_wind()
                    activation = wind_transitions[0]
                else:
                    activation = obs.activate_wind(config["world"]["wind"]["velocity_enu_mps"])
                    activation.update(run_id=config["run_id"], phase=phase)
                    (ROOT / "wind-activation.json").write_text(
                        json.dumps(activation, indent=2) + "\n"
                    )
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
            if decisions and phase in model_phases:
                if phase == "02-D3":
                    # The first entry permission predates model inference; reissue it
                    # from fresh clear observations just before any model authority.
                    decisions.before_authorize = lambda: pad_queue.reconfirm(
                        sample, "before_model_approach_authority"
                    )
                try:
                    permit = execute_model_segments(
                        config,
                        decisions,
                        obs,
                        config["flight_stages"][i + 1]["target_world_xyz_m"],
                        upload,
                        activate,
                        wait_for,
                        run,
                        event,
                        lambda: time.monotonic() - started,
                        prepare_upload=prepare_upload_protocol
                        if config.get("mission_upload_preparation") == UPLOAD_PREPARATION_SCHEMA
                        else None,
                    )
                except Exception as rejection:
                    if not config.get("candidate_recovery"):
                        raise
                    if __package__:
                        from .yokohama_candidate_recovery import recover
                    else:
                        from yokohama_candidate_recovery import recover

                    def recovery_phase(name):
                        nonlocal phase
                        phase = name

                    return recover(
                        config, decisions, rejection, sample=sample, upload=upload,
                        activate=activate, wait_for=wait_for,
                        land=lambda: run([BIN + "commander", "land"]),
                        contacts=lambda: obs.contacts, event=event,
                        clock=lambda: time.monotonic() - started, set_phase=recovery_phase,
                    )
                candidate = permit["candidate"]
                import hashlib

                connector = ROOT / (permit["connector_name"] + "-upload.py")
                if hashlib.sha256(connector.read_bytes()).hexdigest() != permit["connector_sha256"]:
                    raise ValueError("AP connector differs from independently checked route")
                next_connector = permit["connector_name"]
                if config.get("altitude_transport_contract") == SCHEMA:
                    connector_world_hashes[next_connector] = permit["connector_world_items_sha256"]
                if phase == "02-D3":
                    pad_queue.move_hold(candidate["target_world_xyz_m"], permit["permit_id"])
                if phase == model_phases[-1]:
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
                and len(decisions.completed) == len(model_phases)
                and config["decisions"]["backend"] == "native"
            ),
            city_decision_updates=len(decisions.completed) if decisions else 0,
            city_decision_backend=config.get("decisions", {}).get("backend"),
            **(
                {"model_goal_reached": goal_reached(config, decisions.completed[-1]["arrived"])}
                if endpoint_feedback and decisions and decisions.completed
                else {}
            ),
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
            try:
                if altitude_collector:
                    altitude_collector.close()
                    event(
                        "altitude_collector_closed",
                        error_type=altitude_collector.error,
                        thread_alive=altitude_collector.thread.is_alive(),
                    )
            finally:
                events.close()
                trajectory.close()

"""Reopen and independently check PX4 execution, transport and permission records."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct

from scripts.ship_vla_mavlink import decode, POSITION_YAW_MASK
from .ship_vla_adapter import content_hash, snapshot_from_px4
from .ship_vla_execution import (
    authorize_candidate,
    at_target,
    check_execution_sample,
    require_contract,
)


def verify_mode_ack_windows(commands, acks):
    """Allow replicated accepted ACKs, but require each distinct command window."""
    if [mode for _, mode in commands] != [6, 4] or commands[1][0] <= commands[0][0]:
        raise ValueError("mode_command_ack_chain_missing")
    windows = [[], []]
    for ack in acks:
        matches = [
            i
            for i, (sent, _) in enumerate(commands)
            if sent <= ack <= sent + 3 and (i == 1 or ack < commands[1][0])
        ]
        if len(matches) != 1:
            raise ValueError("mode_ack_outside_command_window")
        windows[matches[0]].append(ack)
    if any(not window for window in windows):
        raise ValueError("mode_command_ack_chain_missing")
    return windows


def verify_execution(root, config, samples, events):
    root = Path(root)
    contract = require_contract(config)
    native = contract["proposal_source"] == "fresh_native_aerovla"
    path = root / "vla-execution.json"
    receipt = json.loads(path.read_text())
    if (
        receipt.get("schema_version") != "ship_vla_execution.v1"
        or receipt.get("status") != "observed_complete"
        or receipt.get("vla_inference_invoked") is not native
        or receipt.get("dispatch_invoked") is not True
        or receipt.get("physical_execution_invoked") is not False
        or receipt.get("proposal_source") != contract["proposal_source"]
    ):
        raise ValueError("execution_receipt_not_complete")
    native_chain = None
    if native:
        from .ship_aerovla_host import verify_native_chain

        native_chain = verify_native_chain(root, config, samples, receipt)
    named = {}
    for name in (
        "urban_entry_observed",
        "vla_execution_started",
        "vla_execution_observed",
        "urban_actor_started",
        "vla_mission_resumed",
    ):
        found = [e for e in events if e["event"] == name]
        if len(found) != 1 or found[0].get("run_id") != config["run_id"]:
            raise ValueError("execution_event_missing_or_duplicated")
        named[name] = found[0]
    times = [e["elapsed_s"] for e in named.values()]
    if (
        times != sorted(times)
        or named["vla_execution_observed"]["receipt_sha256"]
        != hashlib.sha256(path.read_bytes()).hexdigest()
    ):
        raise ValueError("execution_event_binding_mismatch")
    source_root = Path(__file__).resolve().parents[2]
    for name in (
        "ship_vla_adapter.py",
        "ship_vla_execution.py",
        "ship_vla_executor.py",
        "ship_vla_mavlink.py",
    ):
        local = (
            source_root
            / (
                "scripts"
                if name in ("ship_vla_executor.py", "ship_vla_mavlink.py")
                else "src/runtime"
            )
            / name
        )
        digest = config["urban"]["onboard_source_sha256"][name]
        for p in (local, root / name, root / "sources" / name):
            if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise ValueError("execution_source_binding_mismatch")
    by_time = {r["elapsed_s"]: r for r in samples}
    if len(by_time) != len(samples):
        raise ValueError("duplicate_telemetry_time")
    resumed = by_time[named["vla_mission_resumed"]["source_sample_elapsed_s"]]
    resumed_state = snapshot_from_px4(resumed, config)
    if (
        resumed_state["nav_state"] != 3
        or not resumed_state["position_valid"]
        or max(resumed_state["pose_age_s"], resumed_state["image_age_s"]) > 0.6
        or math.hypot(*resumed_state["velocity_ned_mps"]) <= 0.5
        or not 0 <= named["vla_mission_resumed"]["elapsed_s"] - resumed["elapsed_s"] < 0.2
    ):
        raise ValueError("mission_motion_handoff_not_observed")
    for kind, reused in (
        ("outbound_upload", False),
        ("urban_upload", True),
        ("return_upload", True),
    ):
        upload = [e for e in events if e["event"] == kind]
        if len(upload) != 1 or upload[0]["receipt"].get("mavlink_session_reused") is not reused:
            raise ValueError("mission_mavlink_session_contract_mismatch")
    permits = {}
    initial = {}
    used = [resumed]
    for key in ("prestream", "execution"):
        value = receipt[key]
        first, current = (by_time[value[k]] for k in ("input_elapsed_s", "current_elapsed_s"))
        expected = authorize_candidate(
            config,
            value["proposal"],
            first,
            current,
            now_s=value["permit"]["issued_at_s"],
            prestream=native and key == "prestream",
            inference=receipt["inference"] if native and key == "execution" else None,
        )
        if value["permit"] != expected:
            raise ValueError("execution_permit_not_reproducible")
        permits[key] = expected
        initial[key] = snapshot_from_px4(first, config)
        used.extend((first, current))
    records = receipt["samples"]
    order = ["prestream", "offboard", "rotate", "translate", "settle", "loiter"]
    if sorted(set(r["stage"] for r in records), key=order.index) != order:
        raise ValueError("execution_stages_missing")
    if [order.index(r["stage"]) for r in records] != sorted(
        order.index(r["stage"]) for r in records
    ):
        raise ValueError("execution_stages_out_of_order")
    states = []
    prior = None
    for record in records:
        row = by_time[record["elapsed_s"]]
        used.append(row)
        if record["row_sha256"] != content_hash(row):
            raise ValueError("execution_sample_hash_mismatch")
        if (
            prior is not None
            and not 0 < row["elapsed_s"] - prior <= contract["observation_timeout_s"]
        ):
            raise ValueError("execution_observation_gap")
        prior = row["elapsed_s"]
        key = "prestream" if record["stage"] == "prestream" else "execution"
        modes = (
            {4}
            if key == "prestream"
            else {4, 14}
            if record["stage"] in ("offboard", "loiter")
            else {14}
        )
        state = check_execution_sample(row, initial[key], permits[key], config, modes=modes)
        states.append((record["stage"], row, state))
    if not times[1] <= records[0]["elapsed_s"] <= records[-1]["elapsed_s"] <= times[2]:
        raise ValueError("execution_sample_event_order")
    for row in used:
        if row["run_id"] != config["run_id"] or row["world_sha256"] != config["world_sha256"]:
            raise ValueError("execution_sample_identity_mismatch")
        frame = row["onboard_frame"]
        p = (root / frame["file"]).resolve()
        if (
            not p.is_relative_to(root.resolve())
            or hashlib.sha256(p.read_bytes()).hexdigest() != frame["sha256"]
        ):
            raise ValueError("execution_image_hash_mismatch")
    candidate = permits["execution"]["candidate"]
    if math.dist(resumed["local_ned"], candidate["target_ned_m"]) < 3:
        raise ValueError("mission_departure_not_observed")
    rotated = []
    for stage, row, state in states:
        if stage != "rotate":
            continue
        aligned = (
            abs(
                math.remainder(
                    state["heading_ned_rad"] - candidate["target_heading_ned_rad"], 2 * math.pi
                )
            )
            <= contract["yaw_tolerance_rad"]
            and math.dist(state["position_ned_m"], candidate["start_ned_m"])
            <= contract["target_tolerance_m"]
            and math.hypot(*state["velocity_ned_mps"]) <= contract["settle_speed_mps"]
        )
        rotated = [*rotated, row] if aligned else []
    if (
        len(rotated) < 2
        or rotated[-1]["elapsed_s"] - rotated[0]["elapsed_s"] < 0.5
        or not 0 <= receipt["translation_started_at_s"] - rotated[-1]["elapsed_s"] < 0.1
    ):
        raise ValueError("yaw_alignment_before_translation_not_verified")
    for stage, field in (
        ("offboard", "offboard_observed_at_s"),
        ("settle", "target_stable_at_s"),
        ("loiter", "loiter_stable_at_s"),
    ):
        last = next((r for name, r, s in reversed(states) if name == stage), None)
        if (
            last is None
            or last["elapsed_s"] != receipt[field]
            or (stage == "offboard" and last["nav_state"] != 14)
        ):
            raise ValueError("execution_milestone_not_bound_to_state")
    for stage, seconds in (("settle", contract["settle_s"]), ("loiter", 1.0)):
        tail = []
        for name, row, state in states:
            if name != stage:
                continue
            okay = at_target(state, candidate, contract) and (
                stage != "loiter" or state["nav_state"] == 4
            )
            tail = [*tail, row] if okay else []
        if len(tail) < 2 or tail[-1]["elapsed_s"] - tail[0]["elapsed_s"] < seconds:
            raise ValueError("target_or_handoff_stability_not_observed")
    rows = [r for stage, r, s in states if stage != "prestream"]
    moved = math.dist(initial["execution"]["position_ned_m"], rows[-1]["local_ned"])
    if (
        moved
        < math.dist(candidate["start_ned_m"], candidate["target_ned_m"])
        - contract["target_tolerance_m"]
    ):
        raise ValueError("proposed_displacement_not_observed")
    transport = [
        json.loads(line) for line in (root / "vla-transport.jsonl").read_text().splitlines()
    ]
    sent = []
    commands = []
    acks = []
    for item in transport:
        raw = bytes.fromhex(item["frame_hex"])
        decoded = decode(raw)
        if len(decoded) != 1:
            raise ValueError("invalid_transport_frame_crc")
        packet = decoded[0]
        if item["direction"] == "sent":
            if item["bytes_sent"] != len(raw) or (packet["system"], packet["component"]) != (
                255,
                191,
            ):
                raise ValueError("transport_source_or_length_mismatch")
            matching_permits = [
                v for v in permits.values() if content_hash(v) == item["permit_sha256"]
            ]
            if (
                len(matching_permits) != 1
                or not matching_permits[0]["issued_at_s"]
                <= item["elapsed_s"]
                <= matching_permits[0]["expires_at_s"]
            ):
                raise ValueError("transport_permit_binding_or_expiry_mismatch")
            if packet["id"] == 84:
                if len(packet["payload"]) != 53:
                    raise ValueError("invalid_setpoint_payload_length")
                v = struct.unpack("<IfffffffffffHBBB", packet["payload"])
                if (
                    v[-4:] != (POSITION_YAW_MASK, 1, 1, 1)
                    or not all(math.isfinite(x) for x in v[1:12])
                    or any(x != 0 for x in v[4:10])
                    or v[11] != 0
                ):
                    raise ValueError("transport_setpoint_contract_mismatch")
                point = list(v[1:4])
                yaw = v[10]
                t = item["elapsed_s"]
                # Sent positions must lie on the approved straight segment, with
                # only observed prestream drift allowed before the new permit.
                from .ship_vla_execution import distance_to_segment

                matches = [
                    key
                    for key, value in permits.items()
                    if content_hash(value) == item["permit_sha256"]
                ]
                if len(matches) != 1:
                    raise ValueError("transport_permit_binding_mismatch")
                key = matches[0]
                c = permits[key]["candidate"]
                if distance_to_segment(point, c["start_ned_m"], c["target_ned_m"]) > 0.01:
                    raise ValueError("unapproved_transport_position")
                if not permits[key]["issued_at_s"] <= t <= permits[key]["expires_at_s"]:
                    raise ValueError("transport_outside_permit")
                if (
                    t < receipt["translation_started_at_s"]
                    and math.dist(point, c["start_ned_m"]) > 0.1
                ):
                    raise ValueError("translation_before_yaw_confirmation")
                yaw_options = [initial[key]["heading_ned_rad"], c["target_heading_ned_rad"]]
                if min(abs(math.remainder(yaw - y, 2 * math.pi)) for y in yaw_options) > 0.0001:
                    raise ValueError("unapproved_transport_yaw")
                sent.append(item)
            elif packet["id"] == 76:
                if native and matching_permits[0].get("vla_inference_invoked") is not True:
                    raise ValueError("native_mode_command_without_inference_permit")
                if len(packet["payload"]) != 33:
                    raise ValueError("invalid_command_payload_length")
                params = struct.unpack("<fffffffHBBB", packet["payload"])
                if params not in (
                    (1, 6, 0, 0, 0, 0, 0, 176, 1, 1, 0),
                    (1, 4, 3, 0, 0, 0, 0, 176, 1, 1, 0),
                ):
                    raise ValueError("unapproved_transport_command")
                commands.append((item["elapsed_s"], params[1]))
            elif packet["id"] != 0:
                raise ValueError("unapproved_message_type")
        elif item["direction"] == "received":
            p = packet["payload"].ljust(10, b"\0")
            command, result, _, _, system, component = struct.unpack("<HBBiBB", p[:10])
            if (
                packet["id"] != 77
                or (packet["system"], packet["component"]) != (1, 1)
                or (command, result, system, component) != (176, 0, 255, 191)
            ):
                raise ValueError("mode_ack_not_accepted")
            acks.append(item["elapsed_s"])
        else:
            raise ValueError("invalid_transport_direction")
    ack_windows = verify_mode_ack_windows(commands, acks)
    if (
        min(ack_windows[0]) > receipt["offboard_observed_at_s"]
        or min(ack_windows[1]) > receipt["loiter_stable_at_s"]
    ):
        raise ValueError("mode_observed_before_ack")
    for index, field in enumerate(("offboard_requested_at_s", "loiter_requested_at_s")):
        if not 0 <= commands[index][0] - receipt[field] < 0.1:
            raise ValueError("mode_request_timestamp_mismatch")
    if not 0 <= commands[0][0] - permits["execution"]["issued_at_s"] < 0.2:
        raise ValueError("dispatch_not_immediately_revalidated")
    if len(sent) < 30 or sent[-1]["elapsed_s"] - sent[0]["elapsed_s"] > contract["timeout_s"]:
        raise ValueError("transport_interval_invalid")
    gaps = [b["elapsed_s"] - a["elapsed_s"] for a, b in zip(sent, sent[1:])]
    if any(not 0 < g <= 0.5 for g in gaps) or commands[0][0] - sent[0]["elapsed_s"] < 1.0:
        raise ValueError("offboard_stream_rate_or_prestream_invalid")
    last = states[-1][2]
    return {
        "verified": True,
        "execution_scope": "sim",
        "proposal_source": contract["proposal_source"],
        "vla_inference_invoked": native,
        "native_inference_chain": native_chain,
        "dispatch_invoked": True,
        "physical_execution_invoked": False,
        "sample_count": len(records),
        "setpoint_frames_sent": len(sent),
        "mode_acks_verified": 2,
        "mode_ack_frames": len(acks),
        "mission_motion_resumed": True,
        "observed_displacement_m": moved,
        "target_error_m": math.dist(last["position_ned_m"], candidate["target_ned_m"]),
        "altitude_error_m": abs(last["position_ned_m"][2] - candidate["target_ned_m"][2]),
        "target_ned_m": candidate["target_ned_m"],
        "final_ned_m": last["position_ned_m"],
        "maximum_stream_gap_s": max(gaps),
        "clearance_source": "simulator_geometry",
        "duration_s": records[-1]["elapsed_s"] - records[0]["elapsed_s"],
    }

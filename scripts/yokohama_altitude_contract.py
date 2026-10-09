"""Simulator-only world goal to observed PX4 home-relative transport contract.

No home AMSL/world-Z subtraction and no claim to fix estimator bias. The approved
world goal and independent Gazebo safety/receipt/return checks remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct

SCHEMA = "missionos.yokohama-altitude-transport.v1"
MAX_AGE_S = 2.0
MAX_OFFSET_CHANGE_M = 0.05
UPLOAD_PREPARATION_SCHEMA = "missionos.mavlink-upload-preparation.v1"


class AltitudeReferenceChanged(ValueError):
    pass


class AltitudeOffsetChanged(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def field(raw, name):
    match = re.search(r"^\s*" + re.escape(name) + r":[ \t]*([^\s,]+)", raw, re.M)
    try:
        value = float(match.group(1)) if match else None
        return value if finite(value) else None
    except ValueError:
        return None


def valid(raw, name):
    match = re.search(r"^\s*" + re.escape(name) + r":[ \t]*([^\s]+)[ \t]*$", raw, re.M)
    return bool(match and match.group(1).lower() in {"true", "1"})


def listener_age(raw):
    match = re.search(r"^\s*timestamp:\s*\d+\s*\(([0-9.+eE-]+) seconds ago\)", raw, re.M)
    try:
        value = float(match.group(1)) if match else None
        return value if finite(value) else None
    except ValueError:
        return None


def reference(row):
    """Reconstruct from recorded raw topics and an explicitly bounded capture.

    PX4 ages use PX4 boot time. Gazebo source stamps use sim time. Capture duration
    uses worker elapsed monotonic time. None is converted to host Unix time.
    Gazebo stats and pose are asynchronous: their recorded spread is bounded by
    two seconds, rather than calling a future-to-stats pose a simultaneous sample.
    Static home age is not dynamic freshness; validity/reference identity matters.
    """
    try:
        capture = row["altitude_capture"]
        begin, end = capture["begin_worker_wall_s"], capture["end_worker_wall_s"]
        a, b = capture["sim_before_s"], row["sim_s"]
        pose = row["vehicle"]
        xyz = pose["xyz"]
        raw = row["raw_px4"]
        local, glob, home = (
            raw[k] for k in ("vehicle_local_position", "vehicle_global_position", "home_position")
        )
        values = [begin, end, a, b, pose["age_s"], pose["sensor_sim_s"], *xyz]
        if len(xyz) != 3 or not all(finite(v) for v in values):
            raise ValueError("Missing finite altitude capture")
        if not (
            0 <= end - begin <= MAX_AGE_S
            and 0 <= b - a <= MAX_AGE_S
            and 0 <= pose["age_s"] <= MAX_AGE_S
            and max(a, b, pose["sensor_sim_s"]) - min(a, b, pose["sensor_sim_s"]) <= MAX_AGE_S
        ):
            raise ValueError("Stale or reversed altitude capture")
        ages = [listener_age(t) for t in (local, glob)]
        stamps = [field(t, "timestamp") for t in (local, glob)]
        if (
            not all(finite(x) and 0 <= x and x + end - begin <= MAX_AGE_S for x in ages)
            or not all(finite(x) and x > 0 for x in stamps)
            or max(stamps) - min(stamps) > MAX_AGE_S * 1e6
        ):
            raise ValueError("Stale PX4 altitude source")
        if not (
            valid(local, "z_valid")
            and valid(local, "z_global")
            and valid(glob, "alt_valid")
            and valid(home, "valid_alt")
        ):
            raise ValueError("Invalid PX4 altitude reference")
        names = {
            "local_ref_alt_m": (local, "ref_alt"),
            "local_ref_timestamp_us": (local, "ref_timestamp"),
            "local_z_reset_counter": (local, "z_reset_counter"),
            "home_alt_m": (home, "alt"),
            "home_local_z_m": (home, "z"),
            "home_timestamp_us": (home, "timestamp"),
            "home_update_count": (home, "update_count"),
            "global_alt_reset_counter": (glob, "alt_reset_counter"),
        }
        origin = {k: field(t, n) for k, (t, n) in names.items()}
        z, alt = field(local, "z"), field(glob, "alt")
        if not all(finite(x) for x in [z, alt, *origin.values()]):
            raise ValueError("Missing finite altitude origin")
        if (
            origin["home_timestamp_us"] <= 0
            or origin["local_ref_timestamp_us"] <= 0
            or abs(origin["local_ref_alt_m"] - origin["home_local_z_m"] - origin["home_alt_m"])
            > 0.01
            or abs(origin["local_ref_alt_m"] - z - alt) > 0.05
        ):
            raise ValueError("Inconsistent PX4 altitude reference")
        relative = alt - origin["home_alt_m"]
        if (
            not finite(row.get("px4_relative_altitude_m"))
            or abs(row["px4_relative_altitude_m"] - relative) > 1e-8
            or row.get("wall_s") != end
        ):
            raise ValueError("Altitude observation fields disagree")
        return dict(
            schema=SCHEMA,
            origin=origin,
            estimated_relative_home_m=relative,
            world_z_m=xyz[2],
            world_minus_relative_m=xyz[2] - relative,
            capture_begin_worker_wall_s=begin,
            capture_end_worker_wall_s=end,
            px4_boot_timestamps_us=stamps,
            source_ages_s=ages,
            gazebo_stats_pose_spread_s=max(a, b, pose["sensor_sim_s"])
            - min(a, b, pose["sensor_sim_s"]),
            simultaneous=False,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Missing altitude transport observation") from exc


def require_reference(row, now_worker_wall_s=None):
    ref = reference(row)
    if now_worker_wall_s is not None and (
        not finite(now_worker_wall_s)
        or not 0 <= now_worker_wall_s - ref["capture_end_worker_wall_s"] <= MAX_AGE_S
    ):
        raise ValueError("Altitude transport observation expired")
    return ref


def executor_altitude(target_world_z, row):
    """Pure arithmetic compatibility helper; dispatch must require_reference."""
    relative = row.get("px4_relative_altitude_m")
    world_z = row.get("vehicle", {}).get("xyz", [None] * 3)[2]
    if not all(finite(v) for v in (relative, world_z, target_world_z)):
        raise ValueError("Missing finite PX4 home-relative altitude mapping")
    return relative + target_world_z - world_z


def mission_wire_items(items):
    """Represent the actual MAVLink float32 and integer coordinate encoding."""
    def f32(value):
        return struct.unpack("<f", struct.pack("<f", value))[0]
    return [[i[0], i[1], int(i[2] * 1e7) / 1e7, int(i[3] * 1e7) / 1e7,
             f32(i[4]), i[5], i[6], *[f32(v) for v in i[7:11]]] for i in items]


def validate_upload_preparation(transaction, preparation):
    if (
        preparation.get("schema") != UPLOAD_PREPARATION_SCHEMA
        or preparation.get("transaction_id") != transaction.get("transaction_id")
        or preparation.get("transaction_sha256") != digest(transaction)
        or not isinstance(transaction.get("transaction_id"), str)
        or len(transaction["transaction_id"]) != 32
        or preparation.get("clear_outcome") not in ("acknowledged", "unconfirmed")
        or preparation.get("clear_ack_type") not in (None, 0)
        or (preparation["clear_outcome"] == "acknowledged") != (preparation["clear_ack_type"] == 0)
        or not finite(preparation.get("prepared_at_worker_wall_s"))
        or preparation["prepared_at_worker_wall_s"] < 0
        or preparation.get("mavlink_session_reused") != transaction.get("reuse_mavlink_session")
    ):
        raise ValueError("Unbound upload preparation")
    return preparation["prepared_at_worker_wall_s"]


def compile_mission(items, row, *, segment, run_id, world_sha256, now_worker_wall_s=None):
    ref = require_reference(row, now_worker_wall_s)
    if row.get("run_id") != run_id or row.get("world_sha256") != world_sha256:
        raise ValueError("Cross-run altitude observation")
    mapped = []
    for item in items:
        if item.get("frame") != 6 or not finite(item.get("world_z_m")):
            raise ValueError("Mission requires world-Z template and frame 6")
        command = item["world_z_m"] - ref["world_minus_relative_m"]
        if not finite(command):
            raise ValueError("Nonfinite mapped altitude")
        wire = [
            item["seq"],
            item["command"],
            item["latitude_deg"],
            item["longitude_deg"],
            command,
            item.get("current", 0),
            6,
            *[item.get("param" + str(i), 0.0) for i in range(1, 5)],
        ]
        if not all(finite(v) for v in wire) or item["seq"] != len(mapped):
            raise ValueError("Invalid or noncontiguous mission template")
        mapped.append(wire)
    if not mapped:
        raise ValueError("Empty mapped mission")
    return dict(
        schema=SCHEMA,
        segment=segment,
        run_id=run_id,
        world_sha256=world_sha256,
        world_items=items,
        mission_items=mapped,
        observation=row,
        observation_sha256=digest(row),
        reference=ref,
        role="planned MAVLink transport; not measured controller setpoint",
    )


def verify_mapping(mapping):
    expected = compile_mission(
        mapping["world_items"],
        mapping["observation"],
        segment=mapping["segment"],
        run_id=mapping["run_id"],
        world_sha256=mapping["world_sha256"],
    )
    if mapping != expected:
        raise ValueError("Altitude transport evidence mismatch")
    return expected["reference"]


def recheck_mapping(mapping, current, now_worker_wall_s=None):
    old = verify_mapping(mapping)
    ref = require_reference(current, now_worker_wall_s)
    if (
        current.get("run_id") != mapping["run_id"]
        or current.get("world_sha256") != mapping["world_sha256"]
    ):
        raise ValueError("Cross-run altitude activation")
    if ref["origin"] != old["origin"]:
        raise AltitudeReferenceChanged("Altitude origin changed during upload")
    if abs(ref["world_minus_relative_m"] - old["world_minus_relative_m"]) > MAX_OFFSET_CHANGE_M:
        raise AltitudeOffsetChanged("World-to-AP altitude mapping expired during upload")
    return ref


def verify_transport_evidence(root, config, events):
    """Check new transport evidence; historical snapshots keep their old claims."""
    if "altitude_transport_contract" not in config:
        return dict(status="legacy", qualification="no unified altitude contract recorded")
    if config["altitude_transport_contract"] != SCHEMA:
        raise ValueError("Unknown altitude transport contract")
    stages = {s["name"]: s for s in config["flight_stages"]}
    prepared = [e for e in events if e["event"] == "altitude_transport_prepared"]
    commands = [e for e in events if e["event"] == "altitude_transport_command_sent"]
    permits = [e["permit"] for e in events if e["event"] == "city_permit_consumed"]
    if not prepared or not commands:
        raise ValueError("Missing altitude dispatch evidence")
    mappings = {}
    receipts = [e for e in events if e["event"] == "upload_receipt"]
    for event in prepared:
        mapping = event["mapping"]
        verify_mapping(mapping)
        identity = digest(mapping)
        name = mapping["segment"]
        if (
            event.get("mapping_sha256") != identity
            or event["segment"] != name
            or mapping["run_id"] != config["run_id"]
            or mapping["world_sha256"] != config["world"]["world_sha256"]
        ):
            raise ValueError("Altitude event binding mismatch")
        if name in stages:
            expected = stages[name]["items"]
        else:
            permit = next(
                (p for p in permits if name in (p["upload_name"], p["connector_name"])), None
            )
            if permit is None:
                raise ValueError("Unapproved altitude mission segment")
            if name == permit["connector_name"]:
                path = root / (name + "-world-items.json")
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != permit["connector_world_items_sha256"]
                ):
                    raise ValueError("Connector world template changed")
                expected = json.loads(path.read_text())
            else:
                if identity != permit["altitude_transport_sha256"]:
                    raise ValueError("City altitude permit changed")
                expected = mapping["world_items"]
                if any(
                    i["world_z_m"] != permit["candidate"]["target_world_xyz_m"][2] for i in expected
                ):
                    raise ValueError("City altitude goal changed")
        if mapping["observation"].get("phase") != event["phase"]:
            raise ValueError("Altitude observation phase mismatch")
        if mapping["world_items"] != expected:
            raise ValueError("Approved world altitude mission changed")
        receipt = next(
            (r for r in receipts if r["segment"] == name and r["wall_s"] >= event["wall_s"]), None
        )
        if (
            receipt is None
            or receipt["receipt"]["mission_ack_type"] != 0
            or receipt["receipt"]["mission_items"] != mapping["mission_items"]
        ):
            raise ValueError("Altitude upload receipt mismatch")
        if config.get("mission_upload_preparation") == UPLOAD_PREPARATION_SCHEMA:
            protocol = next((p for p in events if p["event"] == "upload_protocol_prepared"
                             and p["transaction"]["transaction_id"] == event.get("transaction_id")), None)
            if protocol is None or protocol["segment"] != name or protocol["phase"] != event["phase"]:
                raise ValueError("Missing upload preparation evidence")
            transaction, preparation = protocol["transaction"], protocol["preparation"]
            ready = validate_upload_preparation(transaction, preparation)
            if (
                transaction["run_id"] != mapping["run_id"]
                or transaction["world_sha256"] != mapping["world_sha256"]
                or transaction["segment"] != name
                or not ready <= protocol["wall_s"] <= mapping["observation"]["altitude_capture"]["begin_worker_wall_s"]
                or receipt["receipt"].get("transaction_id") != transaction["transaction_id"]
                or receipt["receipt"].get("transaction_sha256") != digest(transaction)
                or receipt["receipt"].get("mapping_sha256") != identity
                or receipt["receipt"].get("preparation") != preparation
                or receipt["receipt"].get("mission_items_wire") != mission_wire_items(mapping["mission_items"])
            ):
                raise ValueError("Mapping predates preparation or receipt is unbound")
        mappings[identity] = (event, receipt)
    phases = set()
    used = set()
    for command in commands:
        identity = command["mapping_sha256"]
        if identity not in mappings:
            raise ValueError("Altitude command lacks prepared mission")
        event, receipt = mappings[identity]
        if (
            command["segment"] != event["segment"]
            or command["phase"] != event["phase"]
            or not receipt["wall_s"] <= command["dispatched_at_worker_wall_s"] <= command["wall_s"]
        ):
            raise ValueError("Altitude dispatch order mismatch")
        # Require the latest prepared transport for this segment, not a stale upload.
        newer = [
            e
            for e in prepared
            if e["segment"] == event["segment"]
            and event["wall_s"] < e["wall_s"] <= command["dispatched_at_worker_wall_s"]
        ]
        if newer:
            raise ValueError("Superseded altitude mission activated")
        if command["observation"].get("phase") != command["phase"]:
            raise ValueError("Altitude activation phase mismatch")
        if command["observation"].get("phase") != command["phase"]:
            raise ValueError("Altitude activation phase mismatch")
        recheck_mapping(
            event["mapping"], command["observation"], command["dispatched_at_worker_wall_s"]
        )
        # Coverage is the approved mission segment actually commanded. A
        # recovery phase can command an existing exit segment; conversely a
        # city command carrying a stage's phase label cannot replace that stage.
        if command["segment"] in stages:
            phases.add(command["segment"])
        used.add(identity)
    if phases != set(stages):
        raise ValueError("Missing altitude mission phase")
    unused = [e for e in prepared if e["mapping_sha256"] not in used]
    if unused:
        first = config["flight_stages"][0]["name"]
        rebinds = [e for e in events if e["event"] == "altitude_home_rebind_before_takeoff"]
        if (
            len(unused) != 1
            or unused[0]["segment"] != first
            or len(rebinds) != 1
            or rebinds[0]["observation"]["landed"] is not True
            or rebinds[0]["observation"]["arming_state"] != 2
            or rebinds[0]["wall_s"] <= unused[0]["wall_s"]
        ):
            raise ValueError("Unexplained unused altitude upload")
    return dict(
        status="passed",
        prepared=len(prepared),
        command_attempts=len(commands),
        phases=sorted(phases),
        mapping_sha256=sorted(mappings),
        qualification="planned transport and mode commands verified; controller/Gazebo outcomes are separate",
    )

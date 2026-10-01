"""Opt-in simulated cargo and a separate observation-based receiving station.

The receiver observes Gazebo poses/contact, not a human accepting a package.
No release command, transport acknowledgement or model prediction is a receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import threading
import xml.etree.ElementTree as ET


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_jsonl(path):
    if not path.exists():
        return []
    text = path.read_text()
    lines = text.splitlines()
    if text and not text.endswith("\n"):
        lines = lines[:-1]
    return [json.loads(line) for line in lines]


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def extend_world(root, bundle, world):
    from src.runtime.yokohama_scene import camera, sha256, to_world
    from scripts.smoke_missionos_auto_mission_full_runtime_probe import (
        _payload_model_sdf_patch,
        _payload_world_sdf_patch,
    )

    if not world.get("sea_extension"):
        raise ValueError("Cargo extension requires the stationary ship scenario")
    route = world.get("source_route") or json.loads((bundle / "route.json").read_text())
    pad = to_world(route["delivery_pad"]["center_xyz_m"], world["frame"]).tolist()
    ship = world["sea_extension"]["ship_deck_world_xyz_m"]
    path = root / "models/worlds/default.sdf"
    tree = ET.parse(path)
    node = tree.getroot().find("world")
    cargo = ET.fromstring(_payload_world_sdf_patch(payload_mass_kg=0.05))
    cargo.find("pose").text = f"{ship[0]} {ship[1]} {ship[2] + 0.04} 0 0 0"
    node.append(cargo)
    camera_model = ET.SubElement(node, "model", name="delivery_camera")
    ET.SubElement(camera_model, "static").text = "true"
    link = ET.SubElement(camera_model, "link", name="link")
    camera(
        link,
        "delivery_rgb",
        [pad[0] + 5, pad[1] - 5, pad[2] + 3],
        [0, math.atan2(3, math.sqrt(50)), 3 * math.pi / 4],
        "/yokohama/delivery",
        rate_hz=4,
    )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    model_path = root / "models/x500/model.sdf"
    model_tree = ET.parse(model_path)
    model_tree.getroot().find("model").append(ET.fromstring(_payload_model_sdf_patch()))
    model_tree.write(model_path, encoding="utf-8", xml_declaration=True)
    world["world_sha256"] = sha256(path)
    world["model_sha256"] = {
        str(p.relative_to(root)): sha256(p) for p in sorted((root / "models").glob("*/model.*"))
    }
    world["sea_extension"]["payload_release_present"] = True
    world["payload_delivery"] = dict(
        schema="missionos.yokohama-payload.v1",
        entity="delivery_payload",
        mass_kg=0.05,
        size_m=[0.12, 0.12, 0.08],
        pad_world_xyz_m=pad,
        hover_world_xyz_m=[*pad[:2], pad[2] + 3],
        pad_acceptance_radius_m=1.5,
        rest_height_tolerance_m=0.08,
        stability_sim_s=3,
        maximum_motion_m=0.05,
        maximum_speed_mps=0.1,
        maximum_observation_age_s=0.5,
        maximum_contact_age_sim_s=0.5,
        maximum_sample_gap_sim_s=1,
        receipt_max_age_worker_s=5,
        detach_topic="/model/x500_0/delivery_payload/detach",
        joint_topic="/model/x500_0/delivery_payload/state",
        receiver="independent host verifier of simulated pad contact and resting cargo",
        physical_receipt_verified=False,
    )
    return world


def fresh_pose(row, name, max_age=0.5):
    pose = row.get(name) or {}
    xyz = pose.get("xyz", [])
    age = pose.get("age_s", math.inf)
    stamp = pose.get("sensor_sim_s", -math.inf)
    return (
        len(xyz) == 3
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in xyz)
        and isinstance(age, (int, float))
        and 0 <= age <= max_age
        and abs(row["sim_s"] - stamp) <= max_age
        and pose.get("id") is not None
    )


def require_release(config, row):
    payload = config["world"]["payload_delivery"]
    if not (
        row.get("run_id") == config["run_id"]
        and row.get("world_sha256") == config["world"]["world_sha256"]
        and row.get("phase") == "PAYLOAD-LOW"
        and fresh_pose(row, "vehicle")
        and fresh_pose(row, "payload")
        and row.get("nav_state") == 4
        and row.get("arming_state") == 2
        and row.get("landed") is False
        and len(row.get("velocity_ned", [])) == 3
        and math.hypot(*row["velocity_ned"]) <= 0.3
        and math.dist(row["vehicle"]["xyz"], payload["hover_world_xyz_m"]) <= 0.6
        and math.dist(row["vehicle"]["xyz"], row["payload"]["xyz"]) <= 0.8
    ):
        raise ValueError("Cargo release outside approved low, stationary, carrying hold")
    return dict(
        schema="missionos.yokohama-payload-request.v1",
        run_id=config["run_id"],
        config_sha256=digest(config),
        observation=row,
        observation_sha256=digest(row),
        approved_by=config["operator_approval"],
    )


def assess_delivery(config, request, rows, contacts, joints):
    """Pure receiving predicate. Bind to the release and require a fresh stable tail."""
    p = config["world"]["payload_delivery"]
    checks = {}
    if not rows:
        return dict(verified=False, checks={"observations_present": False})
    try:
        anchor = request["observation"]
        checks["release_bound_to_observed_hold"] = request == require_release(
            config, anchor
        ) and any(digest(r) == request["observation_sha256"] for r in rows)
    except (KeyError, ValueError, TypeError):
        return dict(verified=False, checks={"release_bound_to_observed_hold": False})
    identities = (anchor["vehicle"]["id"], anchor["payload"]["id"])
    checks["same_run_and_entities"] = all(
        r.get("run_id") == config["run_id"]
        and r.get("world_sha256") == config["world"]["world_sha256"]
        and (not r.get("payload") or (r["vehicle"]["id"], r["payload"]["id"]) == identities)
        for r in rows
    )
    checks["ordered_finite_clock"] = (
        bool(rows)
        and all(math.isfinite(r["sim_s"]) and math.isfinite(r["wall_s"]) for r in rows)
        and all(
            b["sim_s"] >= a["sim_s"] and b["wall_s"] > a["wall_s"] for a, b in zip(rows, rows[1:])
        )
    )
    carrying = [
        r
        for r in rows
        if r["phase"] == "SEA-INBOUND-COAST"
        and r["sim_s"] < anchor["sim_s"]
        and fresh_pose(r, "vehicle")
        and fresh_pose(r, "payload")
        and math.dist(r["vehicle"]["xyz"], r["payload"]["xyz"]) <= 0.8
    ]
    checks["cargo_carried_over_sea"] = bool(carrying) and (
        math.dist(carrying[0]["payload"]["xyz"], carrying[-1]["payload"]["xyz"]) > 900
    )
    detached = [
        j
        for j in joints
        if j["state"] == "detached" and anchor["sim_s"] <= j["observed_sim_s"] <= rows[-1]["sim_s"]
    ]
    checks["joint_detached_after_request"] = bool(detached)
    stable = []
    relevant_contacts = []
    pad = p["pad_world_xyz_m"]
    for row in rows:
        if row["sim_s"] <= anchor["sim_s"]:
            continue
        contact = [
            c
            for c in contacts
            if c.get("topic") == "delivery_pad"
            and "delivery_payload::" in c["collision1"] + c["collision2"]
            and "delivery_pad::" in c["collision1"] + c["collision2"]
            and 0 <= row["sim_s"] - c["sensor_sim_s"] <= p["maximum_contact_age_sim_s"]
        ]
        okay = (
            row["phase"] == "PAYLOAD-VERIFY"
            and fresh_pose(row, "vehicle")
            and fresh_pose(row, "payload")
            and row["nav_state"] == 4
            and row["arming_state"] == 2
            and row["landed"] is False
            and math.hypot(*row["velocity_ned"]) <= 0.3
        )
        if okay:
            cargo = row["payload"]["xyz"]
            okay = (
                math.dist(cargo[:2], pad[:2]) <= p["pad_acceptance_radius_m"]
                and abs(cargo[2] - pad[2] - p["size_m"][2] / 2) <= p["rest_height_tolerance_m"]
                and math.dist(row["vehicle"]["xyz"], cargo) >= 2
                and math.dist(row["vehicle"]["xyz"], p["hover_world_xyz_m"]) <= 0.6
                and bool(contact)
            )
        if not okay:
            stable = []
            relevant_contacts = []
            continue
        if stable:
            dt = row["sim_s"] - stable[-1]["sim_s"]
            if dt == 0:
                continue
            if (
                dt > p["maximum_sample_gap_sim_s"]
                or math.dist(cargo, stable[-1]["payload"]["xyz"]) / dt > p["maximum_speed_mps"]
                or math.dist(cargo, stable[0]["payload"]["xyz"]) > p["maximum_motion_m"]
            ):
                stable = []
                relevant_contacts = []
        stable.append(row)
        relevant_contacts.append(contact[-1])
    checks["cargo_landed_and_stable_on_pad"] = bool(stable) and (
        stable[-1]["sim_s"] - stable[0]["sim_s"] >= p["stability_sim_s"]
    )
    if not all(checks.values()):
        return dict(verified=False, checks=checks)
    evidence = dict(
        carry_start=carrying[0],
        carry_end=carrying[-1],
        stable_observations=stable,
        pad_contacts=relevant_contacts,
        detached=detached[0],
    )
    return dict(
        verified=True,
        checks=checks,
        entity_ids=list(identities),
        evidence_sha256=digest(evidence),
        evidence=evidence,
        observed_through_wall_s=stable[-1]["wall_s"],
        observed_through_sim_s=stable[-1]["sim_s"],
    )


def make_receipt(config, request, assessment):
    if assessment.get("verified") is not True:
        raise ValueError("Cargo receipt requires observed delivery")
    receipt = dict(
        schema="missionos.yokohama-payload-receipt.v1",
        run_id=config["run_id"],
        config_sha256=digest(config),
        request_sha256=digest(request),
        payload_delivery_verified=True,
        physical_receipt_verified=False,
        assessment=assessment,
    )
    return dict(receipt, receipt_id=digest(receipt))


def require_receipt(config, request, receipt, row):
    own = dict(receipt)
    identity = own.pop("receipt_id", None)
    if not (
        identity == digest(own)
        and receipt.get("schema") == "missionos.yokohama-payload-receipt.v1"
        and receipt.get("run_id") == config["run_id"]
        and receipt.get("config_sha256") == digest(config)
        and receipt.get("request_sha256") == digest(request)
        and receipt.get("payload_delivery_verified") is True
        and receipt.get("physical_receipt_verified") is False
        and receipt.get("assessment", {}).get("verified") is True
        and 0
        <= row["wall_s"] - receipt["assessment"]["observed_through_wall_s"]
        <= config["world"]["payload_delivery"]["receipt_max_age_worker_s"]
    ):
        raise ValueError("Unverified, stale or foreign cargo receipt; return denied")


class PayloadReceiver:
    """Host-side receiver; never publishes a command or changes a simulator pose."""

    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        try:
            while not self.stop_event.wait(0.2):
                request_path = self.root / "payload-request.json"
                if not request_path.exists():
                    continue
                request = json.loads(request_path.read_text())
                result = assess_delivery(
                    self.config,
                    request,
                    read_jsonl(self.root / "flight-trajectory.jsonl"),
                    read_jsonl(self.root / "sensor-events.jsonl"),
                    read_jsonl(self.root / "payload-joint-events.jsonl"),
                )
                atomic_json(self.root / "payload-assessment.json", result)
                if result["verified"]:
                    atomic_json(
                        self.root / "payload-receipt.json",
                        make_receipt(self.config, request, result),
                    )
                    return
        except Exception as exc:
            atomic_json(
                self.root / "payload-receiver-error.json",
                dict(error=type(exc).__name__ + ": " + str(exc)),
            )

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Cargo receiver did not stop")

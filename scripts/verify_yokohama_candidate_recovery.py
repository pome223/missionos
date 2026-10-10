"""Independent offline verdict from PX4 telemetry, Gazebo poses and contact.

Worker outcome strings and container cleanup never establish terminal success.
No model, simulator or Docker operation is performed by this verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    require(not Path(path).is_symlink(), "symlink_evidence")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    require(not Path(path).is_symlink(), "symlink_evidence")
    return json.loads(Path(path).read_text())


def rows(path):
    require(not Path(path).is_symlink(), "symlink_evidence")
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def raw_field(raw, name):
    matches = re.findall(r"^\s*" + re.escape(name) + r":\s*(\S+)", raw, re.M)
    require(len(matches) == 1, "missing_or_ambiguous_raw_telemetry_" + name)
    if matches[0] in ("True", "False", "true", "false"):
        return matches[0].lower() == "true"
    value = float(matches[0])
    require(math.isfinite(value), "nonfinite_raw_telemetry")
    return value


def stable_observed(tail, predicate):
    """Duplicate simulation/pose stamps cannot earn stable-duration credit."""
    start = last = None
    count = 0
    for row in tail:
        if not predicate(row):
            start = last = None
            count = 0
            continue
        if last is not None:
            if (
                row["sim_s"] <= last["sim_s"]
                or row["vehicle"]["sensor_sim_s"] <= last["vehicle"]["sensor_sim_s"]
            ):
                continue
            if row["sim_s"] - last["sim_s"] > 2 or row["wall_s"] - last["wall_s"] > 3:
                start = None
                count = 0
        start = row if start is None else start
        last = row
        count += 1
        if count >= 3 and row["sim_s"] - start["sim_s"] >= 2:
            return row
    raise ValueError("missing_distinct_stable_terminal_observations")


def verify_observations(config, trajectory, poses, contacts, rejection, returned, landed):
    """Check measurements without consulting the worker's verdict or cleanup."""
    import numpy as np

    begin, deadline = rejection["recovery_begin_wall_s"], rejection["recovery_deadline_wall_s"]
    require(
        math.isfinite(begin) and begin >= 0 and deadline == begin + 220, "recovery_deadline_changed"
    )
    require(config["timeout_s"] - 10 >= begin + 220, "recovery_reserve_not_available")
    tail = [r for r in trajectory if r["wall_s"] >= rejection["observation"]["wall_s"]]
    require(len(tail) >= 6 and poses, "recovery_measurements_missing")
    home = config["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]
    goal = config["decisions"]["endpoint_feedback"]["goal_world_xyz_m"]
    anchor = rejection["observation"]
    for a, b in zip(tail, tail[1:]):
        require(b["wall_s"] > a["wall_s"] and b["sim_s"] >= a["sim_s"], "observation_time_reversed")
        require(
            b["sim_s"] - a["sim_s"] <= 2 and b["wall_s"] - a["wall_s"] <= 3,
            "recovery_observation_gap",
        )
    for r in tail:
        values = [
            *r["vehicle"]["xyz"],
            *r["velocity_ned"],
            r["wall_s"],
            r["sim_s"],
            r["vehicle"]["age_s"],
            r["vehicle"]["sensor_sim_s"],
            r["battery_fraction"],
        ]
        require(
            all(type(v) in (int, float) and math.isfinite(v) for v in values),
            "nonfinite_observation",
        )
        require(
            r["wall_s"] <= deadline
            and r["run_id"] == config["run_id"]
            and r["world_sha256"] == config["world"]["world_sha256"]
            and 0 <= r["vehicle"]["age_s"] <= 2
            and r["battery_fraction"] >= 0.2
            and r["position_valid"] is True
            and r["reset_counters"] == anchor["reset_counters"],
            "invalid_recovery_telemetry",
        )
        xyz = np.array(r["vehicle"]["xyz"])
        delta = np.array(goal[:2]) - home[:2]
        t = float(np.clip((xyz[:2] - home[:2]) @ delta / (delta @ delta), 0, 1))
        require(
            np.linalg.norm(xyz[:2] - home[:2] - t * delta) <= 1.5, "recovery_corridor_departure"
        )
        require(
            (-0.2 <= xyz[2] <= home[2] + 0.5)
            if r["phase"] == "recovery_land"
            else abs(xyz[2] - home[2]) <= 0.5,
            "recovery_altitude_departure",
        )
        raw = r["raw_px4"]
        require(
            raw_field(raw["vehicle_status"], "arming_state") == r["arming_state"]
            and raw_field(raw["vehicle_land_detected"], "landed") is r["landed"]
            and raw_field(raw["vehicle_local_position"], "xy_valid") is True,
            "parsed_px4_state_mismatch",
        )
        for topic in ("vehicle_status", "vehicle_land_detected", "vehicle_local_position"):
            age = re.search(r"timestamp:\s*\d+\s*\(([0-9.]+) seconds ago\)", raw[topic])
            require(age is not None and float(age[1]) <= 2, "stale_raw_px4_telemetry")
        pose = min(poses, key=lambda p: abs(p["sensor_sim_s"] - r["vehicle"]["sensor_sim_s"]))
        require(
            all(
                type(v) in (int, float) and math.isfinite(v)
                for v in [pose["sensor_sim_s"], *pose["xyz"]]
            )
            and pose["run_id"] == config["run_id"]
            and pose["source_topic"] == "/world/default/pose/info"
            and abs(pose["sensor_sim_s"] - r["vehicle"]["sensor_sim_s"]) <= 0.15
            and math.dist(pose["xyz"], xyz) <= 0.5,
            "independent_gazebo_pose_mismatch",
        )
    airborne = [
        r for r in tail if r["phase"] == "recovery_return" and r["wall_s"] <= returned["wall_s"]
    ]
    return_row = stable_observed(
        airborne,
        lambda r: (
            math.dist(r["vehicle"]["xyz"][:2], home[:2]) <= 0.25
            and abs(r["vehicle"]["xyz"][2] - home[2]) <= 0.15
            and math.hypot(*r["velocity_ned"]) <= 0.3
            and r["arming_state"] == 2
            and r["landed"] is False
        ),
    )
    terminal = [
        r for r in tail if r["phase"] == "recovery_land" and r["wall_s"] <= landed["wall_s"]
    ]
    final = stable_observed(
        terminal,
        lambda r: (
            r["landed"] is True
            and r["arming_state"] == 1
            and math.dist(r["vehicle"]["xyz"][:2], home[:2]) < 1.5
            and -0.2 <= r["vehicle"]["xyz"][2] <= 0.5
            and math.hypot(*r["velocity_ned"]) <= 0.3
        ),
    )
    require(return_row["wall_s"] < final["wall_s"], "terminal_order_invalid")
    require(
        math.dist(anchor["vehicle"]["xyz"][:2], home[:2]) >= 0.5,
        "no_observed_translation_to_return",
    )
    require(
        any(
            c["topic"] == "launch_pad"
            and "x500" in c["collision1"] + c["collision2"]
            and final["sim_s"] - 2 <= c["sensor_sim_s"] <= final["sim_s"]
            for c in contacts
        ),
        "fresh_launch_contact_missing",
    )
    require(
        not any(
            c["topic"] == "city" and "x500" in c["collision1"] + c["collision2"] for c in contacts
        ),
        "city_contact_observed",
    )
    return {
        "recovery_duration_wall_s": final["wall_s"] - begin,
        "return_observed": True,
        "landing_observed": True,
        "disarm_observed": True,
        "fresh_launch_pad_contact_observed": True,
        "recovery_samples": len(tail),
    }


def verify(root, bundle=None, *, adapted=False):
    root = Path(root)
    try:
        from scripts.yokohama_altitude_contract import verify_transport_evidence
        from scripts.yokohama_candidate_recovery import LIMITS, map_clearance
        from scripts.yokohama_endpoint_feedback import feedback_policy
        from src.runtime.yokohama_execution_service import proposal_digest

        config, result = read(root / "config.json"), read(root / "result.json")
        require(config.get("candidate_recovery") == LIMITS, "recovery_limits_changed")
        feedback = feedback_policy(config)
        require(
            feedback is not None and feedback["entry_world_xyz_m"][:2] == [0, 0],
            "fixed_launch_recovery_changed",
        )
        require(adapted or config.get("endpoint_adapter_trial") is None, "dedicated_endpoint_verifier_required")
        if adapted:
            from scripts.yokohama_native_endpoint_contract import validate_config
            validate_config(config)
            require(result.get("physical_execution_invoked") is False, "scope_changed")
        require(
            adapted or (config["decisions"]["backend"] == "fixture"
            and result.get("vla_invoked") is False
            and result.get("wam_invoked") is False
            and result.get("physical_execution_invoked") is False
            and result.get("gpu_requested") is False),
            "scope_changed",
        )
        require(
            sha(root / "models/worlds/default.sdf") == config["world"]["world_sha256"],
            "world_changed",
        )
        require(
            all(sha(root / name) == value for name, value in result["source_sha256"].items()),
            "runtime_source_changed",
        )
        for name, value in config["world"]["model_sha256"].items():
            require(sha(root / name) == value, "simulator_model_changed")
        map_clearance(config, root / "recovery-map.geojson")
        inspect = read(root / "container-inspect.json")[0]
        require(
            inspect["HostConfig"]["NetworkMode"] == "none"
            and not inspect["HostConfig"].get("DeviceRequests"),
            "container_scope_changed",
        )
        approved_raw = (root.parent / "execution-approval.json").read_bytes()
        approved = json.loads(approved_raw)
        require(
            hashlib.sha256(approved_raw).hexdigest() == config["operator_approval_manifest_sha256"]
            and approved["approval"]["approved_proposal_sha256"]
            == proposal_digest(approved["proposal"])
            and approved["proposal"]["limits"] == LIMITS
            and approved["proposal"]["city_models"] == (config["decisions"]["backend"] if adapted else "fixture")
            and approved["proposal"]["image_id"] == inspect["Image"] == result["image_id"],
            "approval_binding_changed",
        )
        for name, value in result["source_sha256"].items():
            matches = [
                v for p, v in approved["proposal"]["input_sha256"].items() if Path(p).name == name
            ]
            require(value in matches, "runtime_not_in_approved_source_closure")
        require(
            feedback["map_sha256"]
            == approved["proposal"]["input_sha256"][
                "docs/examples/yokohama-urban-scene/collision-footprints.geojson"
            ],
            "unapproved_recovery_map",
        )
        events = rows(root / "flight-events.jsonl")

        def one(name):
            matching = [e for e in events if e["event"] == name]
            require(len(matching) == 1, "missing_or_duplicate_" + name)
            return matching[0]

        rejected, revoked = one("candidate_rejected"), one("model_authority_revoked")
        returned, landed = one("recovery_return_observed"), one("recovery_landing_disarm_observed")
        authorization = one("recovery_return_authorized")
        require(
            (rejected["cycle"] in (1, 2) if adapted else rejected["cycle"] == 2)
            and rejected["wall_s"] <= revoked["wall_s"] < returned["wall_s"] < landed["wall_s"],
            "recovery_event_order_changed",
        )
        require(
            revoked["wall_s"] <= authorization["wall_s"] < returned["wall_s"]
            and authorization["phase"] == "recovery_return"
            and authorization["authority"] == "separate fixed vehicle recovery"
            and authorization["target_world_xyz_m"] == feedback["entry_world_xyz_m"]
            and authorization["deadline_wall_s"] == rejected["recovery_deadline_wall_s"],
            "fixed_return_authority_binding_missing",
        )
        require(
            read(root / "decisions/shutdown.json")["session_revoked"] is True
            and revoked["receipt"]["session_revoked"] is True,
            "host_revocation_missing",
        )
        if adapted and config["decisions"]["backend"] == "native":
            require(read(root / "decisions/shutdown.json").get("remote_model_processes_absent") is True
                    and revoked["receipt"].get("remote_model_processes_absent") is True,
                    "remote_model_shutdown_unconfirmed")
        requests = [read(p) for p in sorted((root / "decisions").glob("*-request.json"))]
        stop_index = next(i for i, r in enumerate(requests) if r["operation"] == "stop")
        require(stop_index == len(requests) - 1, "model_request_after_revocation")
        require(
            any(e["event"] == "city_late_response_rejected" for e in events),
            "revoked_replay_guard_missing",
        )
        dispatches = [e for e in events if e["event"] == "city_segment_dispatched"]
        require(
            (len(dispatches) <= rejected["cycle"] and all(e["wall_s"] < rejected["wall_s"] for e in dispatches))
            if adapted else len(dispatches) == 1,
            "rejected_candidate_dispatched_or_no_prior_segment",
        )
        failed_vla = [
            read(p.with_name(p.name.replace("request", "response")))
            for p in (root / "decisions").glob("*-request.json")
            if read(p).get("cycle") == 2 and read(p)["operation"] == "vla"
        ]
        require(
            adapted or (len(failed_vla) == 1 and "error" in failed_vla[0]), "candidate_rejection_receipt_missing"
        )
        require(
            not any(
                e["event"] == "city_request"
                and e["operation"] != "stop"
                and e["wall_s"] >= rejected["wall_s"]
                for e in events
            ),
            "new_model_authority_during_recovery",
        )
        commands = [
            e
            for e in events
            if e["event"] == "altitude_transport_command_sent"
            and e["segment"] == "01-FEEDBACK-EXIT"
        ]
        require(
            len(commands) == 1 and commands[0]["wall_s"] > revoked["wall_s"],
            "unapproved_return_dispatch",
        )
        verify_transport_evidence(root, config, events)
        metrics = verify_observations(
            config,
            rows(root / "flight-trajectory.jsonl"),
            rows(root / "recovery-poses.jsonl"),
            rows(root / "sensor-events.jsonl"),
            rejected,
            returned,
            landed,
        )
        return {
            "status": "passed",
            "mission_outcome": "failed",
            "recovery_outcome": "passed",
            "cleanup_recorded": result.get("cleanup") is True,
            "physical_execution_invoked": False,
            "native_model_inference": False,
            **metrics,
        }
    except (OSError, ValueError, KeyError, TypeError, StopIteration, IndexError) as exc:
        return {
            "status": "failed",
            "mission_outcome": "failed",
            "recovery_outcome": "unverified",
            "reason": type(exc).__name__ + ": " + str(exc),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    verdict = verify(args.run)
    args.output.write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    return 0 if verdict["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

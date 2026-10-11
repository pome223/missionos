"""Offline full-delivery/recovery evidence; no simulator, model or cloud calls."""

from __future__ import annotations

import argparse
from bisect import bisect_left
import hashlib
import json
import math
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.yokohama_delivery_contract import CONTRACT, arguments, validate_config  # noqa: E402
from scripts.verify_yokohama_candidate_recovery import (  # noqa: E402
    read, rows, require, sha, raw_field, stable_observed,
)
from src.runtime.yokohama_native import digest  # noqa: E402
from src.runtime.yokohama_execution_service import proposal_digest  # noqa: E402
from scripts.yokohama_endpoint_feedback import FIXED  # noqa: E402
from scripts.yokohama_goal_distance_adapter import POLICY  # noqa: E402


def one(events, name, **fields):
    matching = [e for e in events if e["event"] == name
                and all(e.get(k) == v for k, v in fields.items())]
    require(len(matching) == 1, "missing_or_duplicate_event:" + name)
    return matching[0]


def verify_measurements(config, trajectory, poses):
    """Bind parsed PX4 state to raw telemetry and separately recorded Gazebo poses."""
    require(bool(trajectory) and bool(poses), "measurements_missing")
    stamps = [p["sensor_sim_s"] for p in poses]
    require(all(b >= a for a, b in zip(stamps, stamps[1:])), "pose_time_reversed")
    require(all(b["wall_s"] > a["wall_s"] and b["sim_s"] >= a["sim_s"]
                for a, b in zip(trajectory, trajectory[1:])), "trajectory_time_reversed")
    require(len({r["vehicle"]["id"] for r in trajectory}) == 1, "vehicle_changed")
    for row in trajectory:
        require(row["run_id"] == config["run_id"]
                and row["world_sha256"] == config["world"]["world_sha256"], "measurement_binding")
        require(all(type(v) in (int, float) and math.isfinite(v) for v in
                    [row["wall_s"], row["sim_s"], *row["vehicle"]["xyz"],
                     *row["velocity_ned"], row["vehicle"]["age_s"],
                     row["vehicle"]["sensor_sim_s"], row["battery_fraction"]])
                and 0 <= row["vehicle"]["age_s"] <= 2, "stale_or_nonfinite_measurement")
        raw = row["raw_px4"]
        require(raw_field(raw["vehicle_status"], "arming_state") == row["arming_state"]
                and raw_field(raw["vehicle_land_detected"], "landed") is row["landed"]
                and raw_field(raw["vehicle_local_position"], "xy_valid") is row["position_valid"],
                "raw_px4_state_mismatch")
        for topic in ("vehicle_status", "vehicle_land_detected", "vehicle_local_position"):
            age = re.search(r"timestamp:\s*\d+\s*\(([0-9.]+) seconds ago\)", raw[topic])
            require(age is not None and float(age[1]) <= 2, "stale_raw_px4")
        index = bisect_left(stamps, row["vehicle"]["sensor_sim_s"])
        nearby = poses[max(0, index - 1): min(len(poses), index + 1)]
        pose = min(nearby, key=lambda p: abs(p["sensor_sim_s"] - row["vehicle"]["sensor_sim_s"]))
        require(pose["run_id"] == config["run_id"]
                and pose["source_topic"] == "/world/default/pose/info"
                and abs(pose["sensor_sim_s"] - row["vehicle"]["sensor_sim_s"]) <= 0.15
                and math.dist(pose["xyz"], row["vehicle"]["xyz"]) <= 0.5,
                "independent_pose_mismatch")


def verify_authored_route(world, route):
    """Bind authored source coordinates to the immutable scene, independently of targets."""
    import numpy as np
    from src.runtime.yokohama_scene import to_world

    waypoints = route["waypoints"]
    require([p["id"] for p in world["points"]] == [p["id"] for p in waypoints],
            "authored_waypoint_inventory_changed")
    for point, original in zip(world["points"], waypoints):
        require(point["xyz_m"] == original["xyz_m"], "authored_source_waypoint_changed")
        expected = to_world(np.array([original["xyz_m"]]), world["frame"])[0]
        require(np.allclose(expected, point["world_xyz_m"], rtol=0, atol=1e-8),
                "authored_world_waypoint_changed")


def verify_recovery_transport(root, config, events):
    """Verify the entire flown prefix and fixed return, not the aborted delivery."""
    from scripts.yokohama_altitude_contract import verify_transport_evidence

    validate_config(config)
    require(config["delivery_scenario"] in {"candidate_rejected", "timeout"},
            "recovery_transport_requires_failed_delivery_scenario")
    required = {"SEA-TAKEOFF", "SEA-INBOUND-COAST", "00-D1", "01-FEEDBACK-EXIT",
                "SEA-OUTBOUND-COAST", "SEA-RETURN"}
    recovery_config = dict(config, flight_stages=[s for s in config["flight_stages"]
                                                if s["name"] in required])
    return verify_transport_evidence(root, recovery_config, events)


def verify(root, bundle=None):
    root = Path(root).resolve()
    bundle = Path(bundle or REPO / "docs/examples/yokohama-urban-scene")
    try:
        config, result = read(root / "config.json"), read(root / "result.json")
        validate_config(config)
        approval_bytes = (root.parent / "execution-approval.json").read_bytes()
        manifest = json.loads(approval_bytes)
        proposal, approval = manifest["proposal"], manifest["approval"]
        require(
            hashlib.sha256(approval_bytes).hexdigest() == config["operator_approval_manifest_sha256"]
            and approval["approved_proposal_sha256"] == proposal_digest(proposal)
            and proposal.get("schema") == "yokohama.delivery-feedback-proposal.v1"
            and proposal.get("contract") == CONTRACT
            and proposal.get("feedback_limits") == FIXED
            and proposal.get("adapter_policy") == POLICY
            and proposal["scenario"] == config["delivery_scenario"]
            and proposal["city_models"] == config["decisions"]["backend"]
            and proposal["physical_execution_invoked"] is False
            and proposal["simulator_arguments"] == arguments(
                proposal["city_models"], proposal["image_id"], proposal["scenario"],
                proposal.get("native_service_config"))
            and approval["maximum_actual_flight_trials"] == 1
            and all(approval.get(k) for k in ("operator_approval_ref", "actor_session_id", "approved_at")),
            "source_bound_operator_approval",
        )
        require(result["config_sha256"] == digest(config)
                and result.get("physical_execution_invoked") is False
                and result.get("cleanup") is True and result.get("worker_reaped") is True,
                "runtime_config_scope_or_cleanup")
        inspect = read(root / "container-inspect.json")[0]
        require(inspect["Image"] == result["image_id"] == proposal["image_id"]
                and inspect["Config"]["Labels"]["missionos.owner"] == config["run_id"]
                and inspect["HostConfig"]["NetworkMode"] == "none"
                and not inspect["HostConfig"].get("DeviceRequests")
                and not inspect["HostConfig"].get("Devices")
                and result.get("nvidia_device_absent") is True, "owned_cpu_simulator_binding")
        sources = result["source_sha256"]
        require({"yokohama_delivery_contract.py", "yokohama_delivery_recovery.py",
                 "verify_yokohama_delivery_trial.py", "yokohama_goal_distance_adapter.py",
                 "verify_yokohama_endpoint_feedback.py"} <= sources.keys(), "required_sources_missing")
        for name, expected in sources.items():
            approved = [v for p, v in proposal["input_sha256"].items() if Path(p).name == name]
            require(sha(root / name) == expected and expected in approved, "unapproved_runtime_source")
        world = config["world"]
        require(sha(root / "models/worlds/default.sdf") == world["world_sha256"], "world_changed")
        require(all(sha(bundle / n) == v for n, v in world["source_sha256"].items()), "scene_changed")
        verify_authored_route(world, read(bundle / "route.json"))
        require(all(sha(root / n) == v for n, v in world["model_sha256"].items()), "models_changed")
        require(sha(root / "collision-footprints.geojson")
                == proposal["input_sha256"]["docs/examples/yokohama-urban-scene/collision-footprints.geojson"],
                "unapproved_map")
        events, trajectory = rows(root / "flight-events.jsonl"), rows(root / "flight-trajectory.jsonl")
        require(all(b["wall_s"] >= a["wall_s"] for a, b in zip(events, events[1:])), "events_reordered")
        verify_measurements(config, trajectory, rows(root / "recovery-poses.jsonl"))
        from src.runtime.yokohama_payload import require_delivery_mount
        mounted = one(events, "delivery_cargo_mount_observed")
        airborne = one(events, "payload_airborne_observed")
        removed = one(events, "delivery_cargo_support_removed")
        require_delivery_mount(config, mounted["observation"], before_takeoff=True)
        require_delivery_mount(config, airborne["observation"], before_takeoff=False)
        require(all(any(digest(r) == digest(event["observation"]) for r in trajectory)
                    for event in (mounted, airborne)), "cargo_mount_observation_missing_from_trajectory")
        scene = root / "cargo-support-removed-scene.pbtxt"
        require(mounted["wall_s"] < airborne["wall_s"] < removed["wall_s"]
                and removed["entity"] == "delivery_cargo_support"
                and "data: true" in removed["response"]
                and sha(scene) == removed["scene_sha256"]
                and all(f'name: "{name}"' in scene.read_text() for name in ("x500_0", "delivery_payload"))
                and 'name: "delivery_cargo_support"' not in scene.read_text(),
                "cargo_mount_or_support_removal_not_observed")
        native = config["decisions"]["backend"] == "native"
        requests = []
        for path in sorted((root / "decisions").glob("*-request.json")):
            request = read(path)
            require(request["run_id"] == config["run_id"]
                    and request["config_sha256"] == digest(config), "request_binding")
            sent = one(events, "city_request", operation=request["operation"], cycle=request["cycle"])
            require(sent["request_sha256"] == digest(request), "request_event_binding")
            response_path = path.with_name(path.name.replace("-request", "-response"))
            if response_path.exists():
                response = read(response_path)
                require(response["request_sha256"] == digest(request)
                        and response["operation"] == request["operation"]
                        and response["run_id"] == config["run_id"], "response_binding")
            requests.append((request, path, sent))
        require([r[0]["sequence"] for r in requests] == list(range(1, len(requests) + 1)), "request_replay")
        counts = {op: sum(r[0]["operation"] == op for r in requests) for op in ("vla", "wam")}
        require(all(n <= 2 for n in counts.values()), "request_budget_exceeded")
        require(requests[-1][0]["operation"] == "stop", "stop_not_terminal")
        stopped = read(requests[-1][1].with_name(requests[-1][1].name.replace("-request", "-response")))
        shutdown = read(root / "decisions/shutdown.json")
        require("error" not in stopped and stopped["value"]["session_revoked"] is True
                and shutdown["session_revoked"] is True, "shutdown_missing")
        if native:
            require(stopped["value"].get("remote_model_processes_absent") is True
                    and shutdown.get("remote_model_processes_absent") is True, "remote_shutdown_missing")
        else:
            require(result.get("gpu_requested") is False and result.get("vla_invoked") is False
                    and result.get("wam_invoked") is False, "fixture_native_claim")
        revoked = one(events, "city_session_revoked")
        require(one(events, "city_late_response_rejected")["session_revoked"] is True,
                "revoked_live_guard_not_tested")
        require(not any(e["event"] == "city_segment_dispatched" and e["wall_s"] >= revoked["wall_s"]
                        or e["event"] == "city_request" and e["operation"] != "stop"
                        and e["wall_s"] >= revoked["wall_s"] for e in events), "model_reentry_after_revocation")
        from scripts.verify_yokohama_endpoint_feedback import verify as endpoint
        if config["delivery_scenario"] in {"delivery", "wait"}:
            control = endpoint(root)
            require(control["status"] == "passed" and control["observed_goal_arrival_verified"],
                    "independent_endpoint_failed:" + str(control.get("reasons")))
            from scripts.verify_yokohama_sitl import verify as full
            flight = full(root, bundle)
            require(flight["status"] == "passed", "full_delivery_failed:" + str(flight["checks"]))
            require(flight["checks"]["payload_delivery_and_return_order"], "receipt_missing")
            if config["delivery_scenario"] == "wait":
                wait = one(events, "pad_wait_executed")
                entry = one(events, "pad_entry_authorized")
                require(revoked["wall_s"] < wait["wall_s"] < entry["wall_s"], "wait_model_authority_order")
            return dict(status="passed", mission_outcome="passed", recovery_outcome="not_needed",
                        scenario=config["delivery_scenario"], endpoint=control, full_flight=flight,
                        native_model_inference=control["native_model_use_verified"],
                        simulated_payload_receipt_verified=True, model_reentry_prevented=True,
                        raw_model_output_control=False, physical_execution=False)
        require(result["observed"]["status"] == "failed_recovered" and result["mission_outcome"] == "failed",
                "failure_was_not_recovered")
        prefix = endpoint(root, recovery_prefix=True)
        require(prefix["status"] == "passed", "prior_model_chain_failed:" + str(prefix.get("reasons")))
        rejection = one(events, "candidate_rejected")
        authority = one(events, "model_authority_revoked")
        require(rejection["cycle"] == 2 and rejection["wall_s"] <= revoked["wall_s"] <= authority["wall_s"],
                "rejection_revocation_order")
        second = next(r for r in requests if r[0]["cycle"] == 2 and r[0]["operation"] == "vla")
        second_response = read(second[1].with_name(second[1].name.replace("-request", "-response")))
        if config["delivery_scenario"] == "candidate_rejected":
            require("error" in second_response, "rejection_not_observed")
            failed_folder = second[1].with_name(second[1].name.removesuffix("-request.json"))
            raw = read(failed_folder / "native-response.json")
            rejected_adjustment = read(failed_folder / "vehicle-distance-adjustment-rejected.json")
            from src.runtime.yokohama_native import vla_candidate
            require(raw.get("fixture") is True and raw["generated_text"] == "98 49 49"
                    and rejected_adjustment["original_candidate"]
                    == vla_candidate(raw["generated_text"], second[0]["observation"])
                    and rejected_adjustment["vla_response_sha256"] == digest(raw)
                    and rejected_adjustment["executed_candidate"] is None,
                    "raw_inadmissible_candidate_not_bound")
        else:
            require(second[2]["wall_s"] + 75 <= rejection["wall_s"]
                    and "exchange deadline: vla" in rejection["reason"], "timeout_not_observed")
            require(second_response["elapsed_s"] >= 80 and not any(
                e["event"] == "city_response" and e.get("operation") == "vla"
                and e.get("cycle") == 2 for e in events), "late_response_accepted")
        require(counts == {"vla": 2, "wam": 1}, "fault_request_budget_changed")
        begin, deadline = rejection["recovery_begin_wall_s"], rejection["recovery_deadline_wall_s"]
        require(deadline == begin + CONTRACT["recovery_timeout_s"]
                and config["timeout_s"] - 10 >= begin + CONTRACT["recovery_timeout_s"], "recovery_deadline_changed")
        transport = verify_recovery_transport(root, config, events)
        require(transport["status"] == "passed", "recovery_altitude_transport_failed")
        from scripts.yokohama_delivery_recovery import validate_sample
        from scripts.yokohama_delivery_contract import qualify_return_map
        require(config["delivery_recovery_map_qualification"] == qualify_return_map(
            config, root / "collision-footprints.geojson"), "return_map_changed")
        tail = [r for r in trajectory if r["wall_s"] >= rejection["observation"]["wall_s"]]
        require(bool(tail) and tail[-1]["wall_s"] <= deadline, "recovery_deadline_exceeded")
        for row in tail:
            validate_sample(config, row, rejection["observation"], landing=row["phase"] == "delivery_recovery_land")
        from scripts.verify_yokohama_endpoint_feedback import map_clearance
        features = read(root / "collision-footprints.geojson")["features"]
        for first, second_row in zip(tail, tail[1:]):
            map_clearance(first["vehicle"]["xyz"], second_row["vehicle"]["xyz"],
                          features, config["world"]["frame"], 1)
        require(not any(c["topic"] == "city" and "x500" in c["collision1"] + c["collision2"]
                        for c in rows(root / "sensor-events.jsonl")), "city_collision_observed")
        stages = {s["name"]: s for s in config["flight_stages"]}
        previous = authority["wall_s"]
        for phase, name in (("delivery_recovery_entry", "01-FEEDBACK-EXIT"),
                            ("delivery_recovery_coast", "SEA-OUTBOUND-COAST"),
                            ("delivery_recovery_ship", "SEA-RETURN")):
            authorized = one(events, "delivery_recovery_leg_authorized", upload_name=name)
            arrived = one(events, "delivery_recovery_leg_observed", upload_name=name)
            command = one(events, "altitude_transport_command_sent", segment=name)
            target = stages[name]["target_world_xyz_m"]
            require(previous < authorized["wall_s"] < command["wall_s"] < arrived["wall_s"]
                    and authorized["target_world_xyz_m"] == target
                    and authorized["deadline_wall_s"] == deadline, "recovery_leg_authority_order")
            observed = stable_observed([r for r in tail if r["phase"] == phase and r["wall_s"] <= arrived["wall_s"]],
                lambda r: math.dist(r["vehicle"]["xyz"][:2], target[:2]) <= 0.25
                and abs(r["vehicle"]["xyz"][2] - target[2]) <= 0.15
                and math.hypot(*r["velocity_ned"]) <= 0.3 and r["arming_state"] == 2 and r["landed"] is False)
            require(digest(observed) == digest(arrived["observation"]), "recovery_arrival_forged")
            previous = arrived["wall_s"]
        landed = one(events, "recovery_landing_disarm_observed")
        home = stages["SEA-RETURN"]["target_world_xyz_m"]
        final = stable_observed([r for r in tail if r["phase"] == "delivery_recovery_land"],
            lambda r: r["landed"] is True and r["arming_state"] == 1
            and math.dist(r["vehicle"]["xyz"][:2], home[:2]) < 1.5
            and -0.2 <= r["vehicle"]["xyz"][2] <= 0.5 and math.hypot(*r["velocity_ned"]) <= 0.3)
        require(previous < landed["wall_s"] <= deadline and digest(final) == digest(landed["observation"]),
                "terminal_ship_landing_forged")
        require(any(c["topic"] == "launch_pad" and "x500" in c["collision1"] + c["collision2"]
                    and final["sim_s"] - 2 <= c["sensor_sim_s"] <= final["sim_s"]
                    for c in rows(root / "sensor-events.jsonl")), "fresh_deck_contact_missing")
        require(not (root / "payload-receipt.json").exists() and not any(e["event"] in {"payload_received", "payload_return_authorized"} for e in events),
                "failed_delivery_claimed_receipt")
        return dict(status="passed", scenario=config["delivery_scenario"], mission_outcome="failed",
                    recovery_outcome="passed", recovery_duration_wall_s=landed["wall_s"] - begin,
                    prior_model_chain=prefix, model_reentry_prevented=True,
                    native_model_inference=False, simulated_payload_receipt_verified=False,
                    raw_model_output_control=False, physical_execution=False)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, IndexError) as exc:
        return dict(status="failed", reason=type(exc).__name__ + ": " + str(exc),
                    physical_execution=False, simulated_payload_receipt_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    verdict = verify(args.run)
    args.output.write_text(json.dumps(verdict, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: v for k, v in verdict.items() if k not in {"endpoint", "full_flight", "prior_model_chain"}}))
    return 0 if verdict["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

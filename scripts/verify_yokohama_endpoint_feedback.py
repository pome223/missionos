"""Reopen a bounded endpoint-feedback run without invoking models or vehicles.

Control, observed goal arrival, sampled map clearance and recorded cleanup are
separate verdicts. A fixture receipt never proves native inference or flight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import ship_anwm  # noqa: E402
from scripts.yokohama_decision_host import validate_native_services  # noqa: E402
from scripts.yokohama_endpoint_feedback import feedback_policy  # noqa: E402
from src.runtime.ship_aerovla_host import validate_service_identity  # noqa: E402
from src.runtime.yokohama_native import (  # noqa: E402
    digest, executor_heading, forecast_consistency, load_capture, past_view, vla_candidate,
)


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def bound_path(root, relative):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute(),
            "invalid_evidence_path")
    path = root / relative
    require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
            "evidence_path_outside_run")
    return path


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def distance_to_segment(point, start, end):
    delta = np.asarray(end, float) - start
    square = float(delta @ delta)
    fraction = 0 if not square else max(0, min(1, float((np.asarray(point) - start) @ delta) / square))
    return math.dist(point, np.asarray(start) + fraction * delta)


def physical_heading(row):
    w, x, y, z = row["vehicle"]["quat_wxyz"]
    require(abs(w*w + x*x + y*y + z*z - 1) <= 1e-5, "invalid_observed_quaternion")
    return math.atan2(1 - 2*(y*y + z*z), 2*(x*y + w*z))


def at_target(row, candidate, permit):
    target = candidate["target_world_xyz_m"]
    return (
        math.dist(row["vehicle"]["xyz"], target) <= 0.25
        and abs(row["vehicle"]["xyz"][2] - target[2]) <= 0.15
        and math.hypot(*row["velocity_ned"]) <= 0.3
        and row["nav_state"] == 4
        and abs(math.remainder(row["heading_ned_rad"] - permit.get(
            "executor_heading_ned_rad", candidate["target_heading_ned_rad"]), 2*math.pi)) <= 0.05
        and abs(math.remainder(physical_heading(row) - candidate["target_heading_world_ned_rad"],
                               2*math.pi)) <= 0.05
    )


def map_clearance(start, end, features, frame, radius):
    """Recompute swept clearance from source-map polygons, never Rules.allowed."""
    from shapely.geometry import LineString, shape

    matrix = np.asarray(frame["source_to_world_matrix"], float)
    points = np.asarray([start, end], float) @ np.linalg.inv(matrix).T
    points += np.asarray(frame["source_origin_xyz_m"], float)
    a, b = points
    relevant = [shape(f["geometry"]) for f in features
                if f["properties"]["zmax"] >= min(a[2], b[2]) - radius
                and f["properties"]["zmin"] <= max(a[2], b[2]) + radius]
    require(bool(relevant), "map_vertical_coverage_missing")
    line = LineString([a[:2], b[:2]])
    clearance = min(line.distance(polygon) for polygon in relevant)
    require(clearance > radius, "sampled_map_sweep_intersects_obstacle")
    return float(clearance)


def verify(root):
    root = Path(root).resolve()
    checks, reasons, details = {}, [], []
    native = False
    goal_observed = False
    try:
        config, result = read(root / "config.json"), read(root / "result.json")
        policy = feedback_policy(config)
        require(policy is not None, "endpoint_feedback_policy_missing")
        native = config["decisions"]["backend"] == "native"
        require(config["decisions"]["backend"] in {"fixture", "native"}, "unknown_model_backend")
        require(result.get("config_sha256") == digest(config), "result_config_binding")
        if not native:
            require(result.get("fixture") is True and result.get("execution_scope") == "cpu_fixture"
                    and result.get("native_model_calls") == {"vla": 0, "wam": 0}
                    and all(result.get(key) is False for key in (
                        "native_vla_invoked", "native_wam_invoked", "physical_execution_invoked", "sitl_invoked")),
                    "fixture_execution_claim_mismatch")
        events = [json.loads(line) for line in (root / "flight-events.jsonl").read_text().splitlines()]
        rows = [json.loads(line) for line in (root / "flight-trajectory.jsonl").read_text().splitlines()]
        require(bool(events) and bool(rows), "observed_trace_missing")
        for sequence, field in ((events, "wall_s"), (rows, "wall_s"), (rows, "sim_s")):
            require(all(type(row[field]) in (int, float) and math.isfinite(row[field]) and row[field] >= 0
                        for row in sequence), "invalid_trace_time")
            require(all(b[field] >= a[field] for a, b in zip(sequence, sequence[1:])), "reordered_trace")

        def one(name, **fields):
            found = [event for event in events if event["event"] == name
                     and all(event.get(k) == v for k, v in fields.items())]
            require(len(found) == 1, "missing_or_duplicate_event:" + name)
            return found[0]

        # Takeoff and the separately authorized AP exit are outside this narrow
        # feedback verdict. Every intermediate session sample remains checked.
        start_requests = [read(path) for path in (root / "decisions").glob("*-request.json")
                          if read(path).get("operation") == "start"]
        require(len(start_requests) == 1, "feedback_start_request_count")
        session_begin = start_requests[0]["observation"]["wall_s"]
        session_end = one("city_endpoint_feedback_completed")["wall_s"]
        all_observed = {digest(row): row for row in rows}
        rows = [row for row in rows if session_begin <= row["wall_s"] <= session_end]
        require(bool(rows), "feedback_observations_missing")

        sources = result.get("source_sha256", {})
        require(bool(sources), "source_manifest_missing")
        for filename, expected in sources.items():
            require(file_hash(bound_path(root, filename)) == expected, "source_snapshot_mismatch")
        required_sources = {"yokohama_decision_worker.py", "yokohama_decision_host.py",
                            "yokohama_flight_worker.py", "yokohama_endpoint_feedback.py",
                            "yokohama_native.py"}
        require(required_sources <= {Path(name).name for name in sources}, "source_manifest_incomplete")
        checks["source_binding"] = True
        map_path = root / "collision-footprints.geojson"
        map_sha = file_hash(map_path)
        require(map_sha == policy["map_sha256"] == config["world"]["source_sha256"]["collision-footprints.geojson"],
                "approved_map_binding")
        features, frame = read(map_path)["features"], config["world"]["frame"]
        entry, goal = policy["entry_world_xyz_m"], policy["goal_world_xyz_m"]
        observed = {digest(row): row for row in rows}
        require(math.dist(start_requests[0]["observation"]["vehicle"]["xyz"], entry) <= 0.25,
                "initial_feedback_hold_outside_entry")
        for row in rows:
            xyz = row["vehicle"]["xyz"]
            require(row["run_id"] == config["run_id"]
                    and row["world_sha256"] == config["world"]["world_sha256"], "trajectory_run_world_binding")
            require(all(type(x) in (int, float) and math.isfinite(x)
                        for x in [*xyz, *row["velocity_ned"], row["heading_ned_rad"], row["vehicle"]["age_s"]]),
                    "nonfinite_observed_state")
            require(row["position_valid"] is True and row["arming_state"] == 2
                    and row["landed"] is False and 0 <= row["vehicle"]["age_s"] <= 2
                    and 0.2 <= row["battery_fraction"] <= 1, "invalid_observed_state")
            physical_heading(row)
            require(distance_to_segment(xyz[:2], entry[:2], goal[:2])
                    <= policy["corridor_half_width_m"] + policy["tracking_tube_m"]
                    and abs(xyz[2] - entry[2]) <= 0.5, "observed_outside_approved_corridor")
        require(len({row["vehicle"]["id"] for row in rows}) == 1, "vehicle_identity_changed")
        require(all(b["wall_s"] - a["wall_s"] <= 2 and b["sim_s"] - a["sim_s"] <= 2
                    for a, b in zip(rows, rows[1:])), "observed_trace_gap")
        checks["trajectory_binding"] = True
        clearances = [map_clearance(a["vehicle"]["xyz"], b["vehicle"]["xyz"], features, frame, 1)
                      for a, b in zip(rows, rows[1:])]
        require(bool(clearances), "insufficient_observed_motion")
        checks["sampled_map_clearance"] = True

        requests = []
        for path in sorted((root / "decisions").glob("*-request.json")):
            request = read(path)
            response = read(path.with_name(path.name.replace("-request", "-response")))
            require(response["request_sha256"] == digest(request)
                    and response["operation"] == request["operation"]
                    and response["run_id"] == request["run_id"] == config["run_id"]
                    and request["config_sha256"] == digest(config) and "error" not in response,
                    "mailbox_binding_or_failure")
            sent = one("city_request", operation=request["operation"], cycle=request["cycle"])
            got = one("city_response", operation=request["operation"], cycle=request["cycle"])
            require(sent["request_sha256"] == digest(request) and got["response_sha256"] == digest(response)
                    and sent["wall_s"] <= got["wall_s"], "mailbox_event_binding")
            if request["operation"] in {"vla", "wam"}:
                require(got["wall_s"] - sent["wall_s"] <= 75
                        and 0 <= response["elapsed_s"] <= 75, "model_request_timeout")
                anchor = request["observation"]
                during = [row for row in rows if sent["wall_s"] <= row["wall_s"] <= got["wall_s"]]
                require(bool(during), "model_wait_observations_missing")
                for row in [anchor, *during]:
                    require(row["nav_state"] == 4 and math.hypot(*row["velocity_ned"]) <= 0.3
                            and math.dist(row["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) <= 0.5
                            and row["reset_counters"] == anchor["reset_counters"]
                            and abs(math.remainder(physical_heading(row) - physical_heading(anchor), 2*math.pi)) <= 0.25,
                            "model_wait_hold_changed")
            require(digest(request["observation"]) in all_observed, "unrecorded_request_observation")
            requests.append((path, request, response, sent, got))
        operations = [r[1]["operation"] for r in requests]
        expected = ["start", "vla", "wam", "authorize", "activate",
                    "vla", "wam", "authorize", "activate", "stop"]
        require(operations == expected, "feedback_request_count_or_order")
        require([r[1]["sequence"] for r in requests] == list(range(1, 11)), "mailbox_sequence_replayed")
        require(requests[-1][4]["wall_s"] - requests[0][3]["wall_s"] <= policy["total_timeout_s"],
                "feedback_total_time_budget")
        identity = requests[0][2]["value"]
        if not native:
            require(identity.get("backend") == "fixture" and identity.get("models_invoked") is False,
                    "fixture_identity_cannot_prove_native")
        else:
            service_sources = {Path(name).name: value for name, value in sources.items()}
            validate_native_services(config, identity["services"], service_sources)
            validate_service_identity(identity["services"]["vla"])
            require(not any(i.get("fixture") is True for i in identity["services"].values()),
                    "native_service_is_fixture")
            wam_identity = identity["services"]["wam"]
            require(wam_identity["model_revision"] == ship_anwm.MODEL_REVISION
                    and wam_identity["vae_revision"] == ship_anwm.VAE_REVISION
                    and wam_identity["diffusion_steps"] == 250, "native_wam_identity")
        checks["finite_request_budget"] = True
        dispatches = [e for e in events if e["event"] == "city_segment_dispatched"]
        arrivals = [e for e in events if e["event"] == "city_segment_arrived"]
        require(len(dispatches) == len(arrivals) == 2, "feedback_action_count")
        if not native:
            activations = [e for e in events if e["event"] == "fixture_executor_activated"]
            uploads = [e for e in events if e["event"] == "fixture_upload_observed"]
            require(len(activations) == len(uploads) == 2
                    and len({e["permit_id"] for e in activations}) == 2
                    and len({e["upload_name"] for e in uploads}) == 2,
                    "fixture_executor_or_upload_count")
        previous = None
        for cycle in (1, 2):
            group = {r[1]["operation"]: r for r in requests if r[1]["cycle"] == cycle}
            vpath, vr, vs, v_sent, _ = group["vla"]
            wpath, wr, ws, w_sent, _ = group["wam"]
            _, ar, ars, _, activate_got = group["activate"]
            _, auth, authorized, _, _ = group["authorize"]
            require(vr["observation"]["phase"] == wr["observation"]["phase"] == policy["entry_phase"],
                    "feedback_left_session_phase")
            require(vr["next_target_world_xyz_m"] == auth["next_target_world_xyz_m"] == goal,
                    "feedback_goal_changed")
            histories = []
            for request in (vr, wr):
                capture = bound_path(root, request["capture"]["file"])
                require(file_hash(capture) == request["capture"]["sha256"], "capture_binding")
                record, arrays = load_capture(capture, appearance=config["decisions"].get("wam_profile") == "motion-v4")
                require(0 <= request["observation"]["sim_s"] - arrays["stamps_ns"][-1] / 1e9 <= 2,
                        "capture_not_fresh_at_request")
                require(math.dist(record["frames"][-1]["pose"]["xyz"], request["observation"]["vehicle"]["xyz"]) <= 0.5,
                        "capture_observation_pose_mismatch")
                if previous:
                    require(record["frames"][0]["stamp_ns"] / 1e9 > previous["arrival"]["sim_s"],
                            "next_capture_not_after_observed_arrival")
                histories.append((record, arrays))
            require(histories[1][0]["frames"][0]["stamp_ns"] > histories[0][0]["frames"][-1]["stamp_ns"],
                    "wam_history_not_new")
            if previous:
                for _, request, _, _, _ in group.values():
                    if request["operation"] != "stop":
                        require(request.get("previous_segment") == previous, "unbound_previous_segment")
                        require(math.dist(request["observation"]["vehicle"]["xyz"], previous["arrival"]["vehicle"]["xyz"]) <= 0.5
                                and request["observation"]["reset_counters"] == previous["arrival"]["reset_counters"],
                                "previous_endpoint_hold_changed")
                require(previous["arrival"]["wall_s"] < v_sent["wall_s"], "decision_before_arrival")
            vfolder = vpath.with_name(vpath.name.removesuffix("-request.json"))
            wfolder = wpath.with_name(wpath.name.removesuffix("-request.json"))
            raw_vla, native_request = read(vfolder / "native-response.json"), read(vfolder / "native-request.json")
            candidate = vla_candidate(raw_vla["generated_text"], vr["observation"])
            require(candidate == vs["value"]["candidate"] and digest(raw_vla) == vs["value"]["vla_response_sha256"],
                    "vla_candidate_rewritten")
            require(native_request["input_row_sha256"] == digest(vr["observation"])
                    and native_request["plan_sha256"] == digest(config)
                    and native_request["future_ground_truth_used"] is False
                    and native_request["dispatch_allowed"] is False, "vla_input_binding")
            capture_parent = bound_path(root, vr["capture"]["file"]).parent
            last_assets = histories[0][0]["frames"][-1]["assets"]
            for key, sensor in (("rgb", "onboard_rgb"), ("down", "down_rgb")):
                asset = last_assets[sensor + "_png"]
                require(native_request["images_sha256"][key] == asset["sha256"]
                        == file_hash(bound_path(capture_parent, asset["file"])), "vla_observed_image_binding")
            if native:
                require(raw_vla.get("fixture") is not True and raw_vla["vla_inference_invoked"] is True
                        and raw_vla["request_sha256"] == digest(native_request)
                        and raw_vla["service"] == identity["services"]["vla"]
                        and raw_vla["input_images_sha256"] == native_request["images_sha256"]
                        and raw_vla["dispatch_invoked"] is False
                        and raw_vla["cuda_allocated_after_request_bytes"] == 0
                        and raw_vla["physical_execution_invoked"] is False, "native_vla_binding")
            else:
                require(raw_vla.get("fixture") is True and raw_vla["vla_inference_invoked"] is False,
                        "fixture_vla_label")
            wam_request, arrays = ship_anwm.validate(wfolder / "input/request.json")
            require(all(np.array_equal(arrays[k], histories[1][1][k]) for k in arrays)
                    and wam_request["vla_response_sha256"] == digest(raw_vla), "wam_input_binding")
            target_pose = ship_anwm.action_pose(arrays["poses"][-1], wam_request["candidates"][1]["delta"])
            vehicle_ned = target_pose[:3, 3] - target_pose[:3, :3] @ [0, -0.1, 0.25]
            require(np.allclose(vehicle_ned[[1, 0, 2]] * [1, 1, -1], candidate["target_world_xyz_m"], atol=1e-7),
                    "wam_candidate_coordinate_binding")
            raw_wam = read(wfolder / "native-response.json") if native else None
            if native:
                require(raw_wam.get("fixture") is not True and raw_wam["wam_inference_invoked"] is True
                        and raw_wam["identity"] == identity["services"]["wam"]
                        and raw_wam["request_sha256"] == file_hash(wfolder / "input/request.json")
                        and raw_wam["history_sha256"] == wam_request["history_sha256"]
                        and raw_wam["cuda_allocated_after_request_bytes"] == 0
                        and raw_wam["physical_execution_invoked"] is False
                        and raw_wam["dispatch_allowed"] is False, "native_wam_binding")
                require([f["candidate"] for f in raw_wam["forecasts"]] == wam_request["candidates"],
                        "native_wam_forecast_set")
            image_checks = []
            for item in wam_request["candidates"]:
                image_path = wfolder / (item["id"] + "-prediction.png")
                predicted = np.array(Image.open(image_path))
                reference, mask = past_view(arrays, ship_anwm.action_pose(arrays["poses"][-1], item["delta"]))
                image_checks.append(dict(candidate=item, **forecast_consistency(predicted, reference, mask)))
                if native:
                    forecast = [f for f in raw_wam["forecasts"] if f["candidate"] == item]
                    require(len(forecast) == 1 and forecast[0]["files"]["prediction"]["sha256"] == file_hash(image_path),
                            "native_prediction_binding")
            require(image_checks == ws["value"]["checks"] and all(c["passed"] for c in image_checks),
                    "image_consistency_not_verified")
            permit = ars["value"]
            require(permit["prepared_permit_sha256"] == digest(authorized["value"])
                    and permit["candidate"] == candidate
                    and permit["vla_response_sha256"] == digest(raw_vla)
                    and permit["wam_assessment_sha256"] == digest(ws["value"])
                    and permit["observation_sha256"] == digest(ar["observation"]), "activated_permit_binding")
            expected_heading = executor_heading(candidate, auth["observation"])
            require(permit["heading_mapping_observation_sha256"] == digest(auth["observation"])
                    and abs(math.remainder(permit["executor_heading_ned_rad"] - expected_heading, 2*math.pi)) < 1e-10,
                    "executor_heading_coordinate_binding")
            start, target = permit["rules"]["start_world_xyz_m"], candidate["target_world_xyz_m"]
            require(start == ar["observation"]["vehicle"]["xyz"]
                    and permit["rules"]["allowed"] is True
                    and permit["rules"]["target_world_xyz_m"] == target, "rules_endpoint_binding")
            origin = (vr["observation"]["vehicle"]["xyz"]
                      if config["decisions"].get("size_bound_origin") == "proposal_observation" else start)
            require(0.5 <= math.dist(origin, target) <= policy["max_segment_m"]
                    and math.dist(origin, goal) - math.dist(target, goal) >= policy["minimum_progress_m"]
                    and abs(target[2] - origin[2]) <= 0.205 and abs(target[2] - entry[2]) <= 0.205
                    and distance_to_segment(target[:2], entry[:2], goal[:2]) <= policy["corridor_half_width_m"],
                    "candidate_outside_goal_corridor")
            map_clearance(start, target, features, frame, policy["clearance_m"])
            consumed = one("city_permit_consumed", permit=permit)
            dispatched = one("city_segment_dispatched", permit_id=permit["permit_id"])
            arrived = one("city_segment_arrived", permit_id=permit["permit_id"])
            require(activate_got["wall_s"] <= consumed["wall_s"] <= dispatched["wall_s"] < arrived["wall_s"]
                    and dispatched["wall_s"] <= permit["expires_at_worker_wall_s"] + 0.2,
                    "permit_dispatch_order_or_expiry")
            require(file_hash(root / (permit["upload_name"] + "-upload.py")) == permit["upload_sha256"],
                    "upload_source_binding")
            if not native:
                uploaded = one("fixture_upload_observed", upload_name=permit["upload_name"])
                executed = one("fixture_executor_activated", permit_id=permit["permit_id"])
                require(uploaded["upload_sha256"] == permit["upload_sha256"]
                        and uploaded["script_executed"] is False
                        and uploaded["wall_s"] <= consumed["wall_s"] <= executed["wall_s"] <= dispatched["wall_s"]
                        and executed["target_world_xyz_m"] == target
                        and executed["start_world_xyz_m"] == start
                        and executed["physical_execution_invoked"] is False, "fixture_executor_receipt_binding")
            path_rows = [row for row in rows if dispatched["wall_s"] <= row["wall_s"] <= arrived["wall_s"]]
            tail = []
            for row in path_rows:
                require(distance_to_segment(row["vehicle"]["xyz"], start, target) <= policy["tracking_tube_m"],
                        "observed_segment_tracking")
                tail = [*tail, row] if at_target(row, candidate, permit) else []
            arrival_row = arrived["observation"]
            require(len({row["sim_s"] for row in tail}) >= 3 and digest(arrival_row) in observed
                    and tail[-1] == arrival_row and tail[-1]["sim_s"] - tail[0]["sim_s"] >= 2,
                    "stable_arrival_not_observed")
            claimed_previous = (next(r[1]["previous_segment"] for r in requests if r[1]["cycle"] == 2
                                     and "previous_segment" in r[1]) if cycle == 1 else None)
            if claimed_previous:
                require(claimed_previous["permit"] == permit and claimed_previous["arrival"] == arrival_row
                        and type(claimed_previous["stable_samples"]) is int
                        and 3 <= claimed_previous["stable_samples"] <= len(tail)
                        and any(row["sim_s"] == claimed_previous["stable_since_sim_s"] for row in tail)
                        and arrival_row["sim_s"] - claimed_previous["stable_since_sim_s"] >= 2,
                        "previous_arrival_stability_forged")
                previous = claimed_previous
            if cycle == 1:
                require(permit.get("connector_name") is None and permit.get("connector_sha256") is None,
                        "intermediate_connector_authority")
                next_sent = next(r[3]["wall_s"] for r in requests if r[1]["cycle"] == 2 and r[1]["operation"] == "vla")
                require(not any(e["event"] in {"upload_receipt", "city_segment_dispatched"}
                                and arrived["wall_s"] < e["wall_s"] < next_sent for e in events),
                        "connector_or_dispatch_between_decisions")
            else:
                require(file_hash(root / (permit["connector_name"] + "-upload.py")) == permit["connector_sha256"],
                        "final_exit_source_binding")
                map_clearance(target, policy["exit_world_xyz_m"], features, frame, policy["clearance_m"])
            details.append({"cycle": cycle, "target_world_xyz_m": target,
                            "arrival_sim_s": arrival_row["sim_s"], "stable_samples": len(tail),
                            "goal_distance_m": math.dist(arrival_row["vehicle"]["xyz"], goal)})
        checks["model_and_permit_chain"] = True
        checks["two_observed_feedback_legs"] = True
        finished, revoked = one("city_endpoint_feedback_completed"), one("city_session_revoked")
        stop = requests[-1]
        require(arrivals[-1]["wall_s"] <= revoked["wall_s"] <= stop[3]["wall_s"] <= stop[4]["wall_s"]
                <= finished["wall_s"], "shutdown_order")
        require(one("city_late_response_rejected")["session_revoked"] is True
                and stop[2]["value"]["session_revoked"] is True, "session_revocation_missing")
        checks["session_revocation"] = True
        checks["recorded_model_shutdown"] = (
            stop[2]["value"].get("remote_model_processes_absent") is True if native
            else stop[2]["value"].get("fixture_stopped") is True
        )
        require(checks["recorded_model_shutdown"], "model_shutdown_receipt_missing")
        goal_observed = details[-1]["goal_distance_m"] <= policy["goal_tolerance_m"]
        require(finished["model_goal_reached"] is goal_observed
                and finished["completed_updates"] == 2
                and finished["goal_world_xyz_m"] == goal
                and finished["exit_world_xyz_m"] == policy["exit_world_xyz_m"], "completion_claim_mismatch")
        require(result.get("status") in {"passed", "completed"}, "producer_reported_failure")
        if not native:
            require(result["fixture_decision_requests"] == {"vla": 2, "wam": 2}
                    and result["executor_action_count"] == result["completed_updates"] == 2
                    and result["model_goal_reached"] is goal_observed, "producer_counter_or_goal_mismatch")
            cleanup = one("fixture_cleanup")
            require(all(cleanup[key] is True and result["cleanup"][key] is True for key in (
                "worker_session_closed", "host_thread_stopped", "session_revoked", "fixture_stopped"))
                and cleanup["worker_active"] is False and result["cleanup"]["worker_active"] is False,
                "fixture_cleanup_record_missing")
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, OSError, OverflowError) as exc:
        reasons.append(type(exc).__name__ + ":" + str(exc))
    control = not reasons
    return {
        "schema_version": "yokohama_endpoint_feedback_verification.v1",
        "status": "passed" if control else "failed", "checks": checks, "reasons": reasons,
        "cycles": details, "control_sequence_verified": control,
        "observed_goal_arrival_verified": control and goal_observed,
        "observed_map_clearance_verified": checks.get("sampled_map_clearance", False),
        "native_model_use_verified": control and native,
        "recorded_model_shutdown_verified": checks.get("recorded_model_shutdown", False),
        "current_remote_cleanup_verified": False, "ap_exit_execution_verified": False,
        "physical_execution_verified": False, "dynamic_obstacle_avoidance_verified": False,
        "whole_mission_completion_verified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verdict = verify(args.run)
    args.output.write_text(json.dumps(verdict, indent=2, allow_nan=False) + "\n")
    print(json.dumps(verdict))
    sys.exit(0 if verdict["status"] == "passed" else 1)

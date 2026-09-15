"""Opt-in real Gazebo/Nav2 episode; no raw robot velocity or physical control.

Run only in the dedicated container created by run.py. The cart actor moves
a synthetic collision box through the simulator pose service. Observed pose
readback, not service ACK or commanded pose, supplies selector inputs.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import struct
import sys
import time
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.tb3_prediction.policy import CANDIDATES, predict_choice


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=["clearing", "lingering"], required=True)
    parser.add_argument(
        "--policy",
        choices=[
            "nav2",
            "constant_velocity",
            "image_only",
            "image_history",
            "nwm",
            "agent_nwm",
            "capture",
            "fixed_wait",
        ],
        required=True,
    )
    parser.add_argument("--refresh-costmaps", action="store_true")
    parser.add_argument("--nav2-detour", action="store_true")
    parser.add_argument("--compact-detour", action="store_true")
    parser.add_argument("--context-period", type=float, choices=[0.25, 0.5], default=0.25)
    parser.add_argument("--actor-initial-y", type=float)
    parser.add_argument("--actor-speed", type=float)
    parser.add_argument("--capture-seconds", type=float, default=20)
    parser.add_argument("--approve-bounded-detour", action="store_true")
    parser.add_argument(
        "--controller-profile",
        choices=["legacy"],
        default="legacy",
    )
    parser.add_argument("--via-heading", choices=["outgoing", "incoming"], default="outgoing")
    args = parser.parse_args()
    if args.via_heading == "incoming" and (
        not args.compact_detour or args.nav2_detour or args.controller_profile != "legacy"
    ):
        parser.error("incoming via heading requires compact detour and legacy controller")
    if args.nav2_detour and args.compact_detour:
        parser.error("choose one persistent-obstacle executor")
    if args.actor_speed is not None and not (0 <= args.actor_speed <= 0.3):
        parser.error("actor speed outside local scene bounds")
    if args.actor_initial_y is not None and not (-1.6 <= args.actor_initial_y <= 1.6):
        parser.error("actor start outside local scene bounds")
    if not (8 <= args.capture_seconds <= 40):
        parser.error("capture duration outside local bound")
    if os.environ.get("RUN_MISSIONOS_TB3_PREDICTION_SIM") != "1":
        parser.error("simulator opt-in required")
    if not Path("/.dockerenv").exists() or os.environ.get("ROS_DOMAIN_ID") != "73":
        parser.error("dedicated simulator container/domain required")
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Image, LaserScan
    from rosgraph_msgs.msg import Clock
    from tf2_msgs.msg import TFMessage
    from ros_gz_interfaces.msg import Contacts, Entity
    from ros_gz_interfaces.srv import SetEntityPose
    from lifecycle_msgs.srv import GetState

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    preparation_deadline = time.monotonic() + 15
    while not (out / "protocol.json").exists():
        if time.monotonic() > preparation_deadline:
            raise RuntimeError("scene_preparation_did_not_finish")
        time.sleep(0.1)
    result = {
        "schema_version": "tb3_prediction_episode.v1",
        "run_id": str(uuid.uuid4()),
        "scenario": args.scenario,
        "policy": args.policy,
        "status": "starting",
        "learned_wam_invoked": False,
        "vla_invoked": False,
        "physical_execution_invoked": False,
        "mission_completion_claimed": False,
        "boundary": "standalone benchmark -> production Nav2 bridge -> Gazebo",
        "operator_authorization": "explicit local simulator benchmark opt-in",
        "contact_scope": "crossing_cart versus waffle_pi only",
        "protocol_sha256": hashlib.sha256((out / "protocol.json").read_bytes()).hexdigest(),
    }
    rclpy.init()
    node = rclpy.create_node("tb3_prediction_observer")
    state = {
        "t": None,
        "robot": None,
        "cart": None,
        "pose_t": None,
        "pose_received_wall": None,
        "odom": None,
        "scan": None,
        "image": None,
        "robot_yaw": None,
        "robot_pose_received_wall": None,
        "image_pose": None,
        "contact_messages": 0,
        "contact_events": [],
        "contact_active": False,
    }
    samples = []
    contacts = []
    actor_receipts = []
    processes = []

    def clock(msg):
        state["t"] = msg.clock.sec + msg.clock.nanosec / 1e9

    def poses(msg):
        for transform in msg.transforms:
            name = transform.child_frame_id
            p = transform.transform.translation
            if name in ("waffle_pi", "crossing_cart"):
                state["robot" if name == "waffle_pi" else "cart"] = [p.x, p.y]
                if name == "waffle_pi":
                    q = transform.transform.rotation
                    state["robot_yaw"] = math.atan2(
                        2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)
                    )
                    state["robot_pose_received_wall"] = time.monotonic()
                state["pose_t"] = transform.header.stamp.sec + transform.header.stamp.nanosec / 1e9
                state["pose_received_wall"] = time.monotonic()

    def odom(msg):
        p = msg.pose.pose.position
        state["odom"] = [p.x, p.y, msg.twist.twist.linear.x, msg.twist.twist.angular.z]
        state["odom_received_wall"] = time.monotonic()

    def camera(msg):
        state["image"] = msg
        if state["robot"] is not None and state["robot_yaw"] is not None:
            state["image_pose"] = {
                "robot_xy_m": state["robot"][:],
                "robot_yaw_rad": state["robot_yaw"],
                "latest_clock_s": state["t"],
                "pose_receipt_age_wall_s": time.monotonic() - state["robot_pose_received_wall"],
                "alignment": "latest Gazebo pose at camera callback; not exact timestamp synchronization",
            }

    def scan(msg):
        values = [r for r in msg.ranges if math.isfinite(r) and msg.range_min < r < msg.range_max]
        state["scan"] = min(values) if values else None

    def contact(msg):
        state["contact_messages"] += 1
        active = False
        for c in msg.contacts:
            names = [c.collision1.name, c.collision2.name]
            if any("waffle_pi" in name for name in names):
                active = True
                contacts.append(
                    {"t_s": state["t"], "collisions": names, "depths_m": list(c.depths)}
                )
        if active and not state["contact_active"]:
            state["contact_events"].append(state["t"])
        state["contact_active"] = active

    _subscriptions = [
        node.create_subscription(Clock, "/clock", clock, 10),
        node.create_subscription(
            TFMessage, "/world/default/pose/info", poses, qos_profile_sensor_data
        ),
        node.create_subscription(Odometry, "/odom", odom, qos_profile_sensor_data),
        node.create_subscription(LaserScan, "/scan", scan, qos_profile_sensor_data),
        node.create_subscription(Image, "/camera/image_raw", camera, qos_profile_sensor_data),
        node.create_subscription(Contacts, "/benchmark/contacts", contact, qos_profile_sensor_data),
    ]
    pose_client = node.create_client(SetEntityPose, "/world/default/set_pose")
    lifecycle_client = node.create_client(GetState, "/bt_navigator/get_state")

    def spin():
        rclpy.spin_once(node, timeout_sec=0.02)

    def wait_for(predicate, timeout_s, reason):
        deadline = time.monotonic() + timeout_s
        while not predicate():
            spin()
            if time.monotonic() > deadline:
                raise RuntimeError(reason)

    def set_cart(y):
        request = SetEntityPose.Request()
        request.entity.name = "crossing_cart"
        request.entity.type = Entity.MODEL
        request.pose.position.x = 0.0
        request.pose.position.y = float(y)
        request.pose.position.z = 0.3
        request.pose.orientation.w = 1.0
        return pose_client.call_async(request)

    def save_image(name):
        msg = state["image"]
        if msg.encoding not in ("rgb8", "bgr8"):
            raise RuntimeError("unsupported camera encoding")
        rows = []
        for y in range(msg.height):
            row = bytearray(msg.data[y * msg.step : y * msg.step + msg.width * 3])
            if msg.encoding == "bgr8":
                row[0::3], row[2::3] = row[2::3], row[0::3]
            rows.append(b"\x00" + row)

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        png = b"\x89PNG\r\n\x1a\n" + chunk(
            b"IHDR", struct.pack(">IIBBBBB", msg.width, msg.height, 8, 2, 0, 0, 0)
        )
        png += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
        (out / name).write_bytes(png)
        return {
            "path": name,
            "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest(),
            "stamp_s": msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9,
            "observed_pose": state["image_pose"],
        }

    def atomic_json(path, value):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(path)

    def observe_clearance():
        from passability_observation import red_exposure

        msg = state["image"]
        return red_exposure(
            bytes(msg.data), msg.width, msg.height, msg.step, bgr=msg.encoding == "bgr8"
        )

    def live_observation():
        msg = state["image"]
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        return {
            "sim_s": state["t"],
            "camera_stamp_sim_s": stamp,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "image_sha256": hashlib.sha256(bytes(msg.data)).hexdigest(),
            "image_hash_encoding": msg.encoding,
            "observed_exposure": observe_clearance(),
            "robot_xy_m": state["robot"][:],
            "robot_stationary": (
                time.monotonic() - state.get("odom_received_wall", 0) < 0.5
                and abs(state["odom"][2]) < 0.01
                and abs(state["odom"][3]) < 0.02
                and 0 <= state["t"] - stamp < 0.5
            ),
        }

    try:
        wait_for(
            lambda: (
                state["t"] is not None
                and state["robot"] is not None
                and state["cart"] is not None
                and state["odom"] is not None
                and state["image"] is not None
                and state["scan"] is not None
                and pose_client.service_is_ready()
                and lifecycle_client.service_is_ready()
            ),
            75,
            "simulator_observation_or_service_not_ready",
        )
        nav2_deadline = time.monotonic() + 45
        while True:
            ready = lifecycle_client.call_async(GetState.Request())
            wait_for(ready.done, 5, "nav2_lifecycle_timeout")
            if ready.result().current_state.id == 3:
                break
            if time.monotonic() > nav2_deadline:
                raise RuntimeError("nav2_not_active")
            until = time.monotonic() + 0.2
            while time.monotonic() < until:
                spin()
        if math.dist(state["robot"], [-1, 0]) > 0.05:
            raise RuntimeError("initial_robot_state_mismatch")
        # Read the running controller, not just the generated configuration.
        from rcl_interfaces.srv import GetParameters

        params_client = node.create_client(GetParameters, "/controller_server/get_parameters")
        try:
            names = [
                "FollowPath.xy_goal_tolerance",
                "general_goal_checker.xy_goal_tolerance",
                "general_goal_checker.yaw_goal_tolerance",
                "FollowPath.max_vel_x",
                "FollowPath.RotateToGoal.lookahead_time",
            ]
            request_params = GetParameters.Request(names=names)
            deadline = time.monotonic() + 5
            while not params_client.service_is_ready():
                spin()
                if time.monotonic() > deadline:
                    raise RuntimeError("controller_parameter_service_timeout")
            pending = params_client.call_async(request_params)
            while not pending.done():
                spin()
                if time.monotonic() > deadline:
                    raise RuntimeError("controller_parameter_readback_timeout")
            values = pending.result().values
            expected = [
                0.05,
                0.25,
                0.25,
                0.20,
                -1.0,
            ]
            if len(values) != len(expected) or any(
                v.type != 3 or abs(v.double_value - e) > 1e-9 for v, e in zip(values, expected)
            ):
                raise RuntimeError("controller_profile_readback_mismatch")
            result["controller_profile_readback"] = dict(
                profile=args.controller_profile,
                parameters=dict(zip(names, [v.double_value for v in values])),
                observed_at_sim_s=state["t"],
                before_mission_start=True,
            )
            atomic_json(
                out / "controller-profile-readback.json", result["controller_profile_readback"]
            )
        finally:
            node.destroy_client(params_client)
        # Scenario parameters belong only to the actor, never to predict_choice.
        initial_y, vy = (-1.1, 0.18) if args.scenario == "clearing" else (0.0, 0.015)
        if args.actor_initial_y is not None:
            initial_y = args.actor_initial_y
        if args.actor_speed is not None:
            vy = args.actor_speed
        result["actor_parameters_for_verification_only"] = {"initial_y": initial_y, "vy": vy}
        future = set_cart(initial_y)
        wait_for(future.done, 4, "initial_actor_apply_timeout")
        if not future.result().success:
            raise RuntimeError("initial_actor_apply_rejected")
        wait_for(
            lambda: abs(state["cart"][1] - initial_y) < 0.02,
            5,
            "actor_materialization_not_observed",
        )
        result["initial_robot_xy_m"] = state["robot"][:]
        result["initial_cart_xy_m"] = state["cart"][:]
        t0 = state["t"]
        actor_next = t0
        sample_next = t0
        pending_pose = None
        actor_error = None

        def tick():
            nonlocal actor_next, sample_next, pending_pose, actor_error
            spin()
            now = state["t"]
            if pending_pose is not None and pending_pose.done():
                ok = pending_pose.result() is not None and pending_pose.result().success
                actor_receipts.append({"observed_at_sim_s": now, "accepted": bool(ok)})
                if not ok:
                    actor_error = "actor_pose_service_rejected"
                pending_pose = None
            if now >= actor_next and pending_pose is None:
                # Stop at the wall; this change is unknown to the baseline predictor.
                y = min(1.6, initial_y + vy * (now - t0))
                pending_pose = set_cart(y)
                actor_next = now + 0.1
            if now >= sample_next:
                if (
                    state["pose_received_wall"] is None
                    or time.monotonic() - state["pose_received_wall"] > 0.5
                ):
                    raise RuntimeError("stale_pose_readback")
                samples.append(
                    {
                        "t_s": now - t0,
                        "robot_xy_m": state["robot"][:],
                        "robot_yaw_rad": state["robot_yaw"],
                        "cart_xy_m": state["cart"][:],
                        "odom": state["odom"][:],
                        "scan_min_m": state["scan"],
                        "pose_stamp_s": state["pose_t"],
                        "time_basis": "latest_sim_clock_at_receipt",
                        "pose_receipt_age_wall_s": time.monotonic() - state["pose_received_wall"],
                    }
                )
                sample_next = now + 0.1
            if args.policy == "agent_nwm":
                atomic_json(out / "live-observation.json", live_observation())
            if actor_error:
                raise RuntimeError(actor_error)

        rpc_sequence = 0

        def agent_rpc(packet):
            nonlocal rpc_sequence
            rpc_sequence += 1
            request_path = out / f"agent-request-{rpc_sequence:03d}.json"
            response_path = out / f"agent-response-{rpc_sequence:03d}.json"
            atomic_json(request_path, packet)
            deadline = time.monotonic() + 150
            while not response_path.exists():
                tick()
                if time.monotonic() > deadline:
                    raise RuntimeError("agent_response_timeout")
            return json.loads(response_path.read_text())

        def fresh_context(attempt):
            frames = []
            deadline = time.monotonic() + 15
            while len(frames) < 4:
                tick()
                stamp = state["image"].header.stamp.sec + state["image"].header.stamp.nanosec / 1e9
                if not frames or stamp - frames[-1]["stamp_s"] >= 0.48:
                    frames.append(save_image(f"agent-context-{attempt}-{len(frames)}.png"))
                if time.monotonic() > deadline:
                    raise RuntimeError("fresh_camera_context_unavailable")
            return frames

        def compute_detour_plan(waypoints, filename):
            from rclpy.action import ActionClient
            from nav2_msgs.action import ComputePathThroughPoses
            from geometry_msgs.msg import PoseStamped
            from scripts.tb3_prediction.route_feasibility import validate_detour_plan

            client = ActionClient(node, ComputePathThroughPoses, "/compute_path_through_poses")

            def await_ready(predicate):
                deadline = time.monotonic() + 8
                while not predicate():
                    tick()  # The actor and observation clock keep advancing during planning.
                    if time.monotonic() > deadline:
                        raise RuntimeError("Nav2_detour_planning_timeout")

            try:
                from nav2_msgs.srv import ClearEntireCostmap

                clearing = node.create_client(
                    ClearEntireCostmap, "/global_costmap/clear_entirely_global_costmap"
                )
                try:
                    await_ready(clearing.service_is_ready)
                    cleared = clearing.call_async(ClearEntireCostmap.Request())
                    await_ready(cleared.done)
                    if cleared.result() is None:
                        raise RuntimeError("detour_costmap_refresh_failed")
                    refresh_at = state["t"]
                    while state["t"] - refresh_at < 0.5:
                        tick()
                finally:
                    node.destroy_client(clearing)
                await_ready(client.server_is_ready)
                goal = ComputePathThroughPoses.Goal()
                goal.planner_id = "GridBased"
                goal.use_start = False
                for x, y in waypoints:
                    pose = PoseStamped()
                    pose.header.frame_id = "map"
                    pose.pose.position.x, pose.pose.position.y = float(x), float(y)
                    pose.pose.orientation.w = 1.0
                    goal.goals.append(pose)
                sent = client.send_goal_async(goal)
                await_ready(sent.done)
                handle = sent.result()
                if not handle.accepted:
                    raise RuntimeError("Nav2_detour_plan_rejected")
                pending = handle.get_result_async()
                await_ready(pending.done)
                response = pending.result()
                plan = {
                    "status": "succeeded" if response.status == 4 else "failed",
                    "frame_id": response.result.path.header.frame_id,
                    "waypoints": waypoints,
                    "computed_at_sim_s": state["t"],
                    "path_xy_m": [
                        [p.pose.position.x, p.pose.position.y] for p in response.result.path.poses
                    ],
                }
                atomic_json(out / filename, plan)
                validate_detour_plan(plan, waypoints, state["t"])
                return plan
            finally:
                client.destroy()

        history_wall = time.monotonic()
        history_images = []
        next_history_image = t0
        while state["t"] - t0 < (args.capture_seconds if args.policy == "capture" else 2):
            tick()
            if state["t"] >= next_history_image:
                history_images.append(save_image(f"history-{len(history_images):02d}.png"))
                next_history_image = state["t"] + 0.25
            if time.monotonic() - history_wall > max(15, args.capture_seconds * 5):
                raise RuntimeError("simulation_clock_stalled")
        history = [
            {"t_s": s["t_s"], "x_m": s["cart_xy_m"][0], "y_m": s["cart_xy_m"][1]} for s in samples
        ]
        input_packet = {
            "history": history,
            "history_images": history_images,
            "robot_xy_m": state["robot"][:],
            "image": save_image("decision.png"),
            "candidates": CANDIDATES,
            "observation_source": "Gazebo pose readback (privileged)",
        }
        (out / "decision-input.json").write_text(json.dumps(input_packet, indent=2))
        if args.policy == "capture":
            result["status"] = "capture_complete"
            result["capture_frames"] = len(history_images) + 1
            return 0
        decision_observation_t = state["t"]
        result["decision_observation_sim_s"] = decision_observation_t - t0
        inference_start = time.monotonic()
        if args.policy == "constant_velocity":
            decision = predict_choice(history, state["robot"])
        else:
            decision = {
                "candidate": "wait" if args.policy == "fixed_wait" else "direct",
                "source": "fixed_policy",
            }
        if args.policy == "fixed_wait":
            decision["wait_time_origin"] = "last_context_timestamp"
        if args.policy in ("image_only", "image_history", "nwm"):
            stride = round(args.context_period / 0.25)
            frames = history_images[-1 - 3 * stride :: stride]
            request = {
                "policy": args.policy,
                "context": [str(out / f["path"]) for f in frames],
                "timestamps": [f["stamp_s"] for f in frames],
            }
            (out / "value-request.json").write_text(json.dumps(request, indent=2))
            while not (out / "value-response.json").exists():
                tick()
                if time.monotonic() - inference_start > 65:
                    raise RuntimeError("prediction_response_timeout")
            decision = json.loads((out / "value-response.json").read_text())
            result["learned_wam_invoked"] = decision["learned_wam_invoked"]
        if args.policy == "agent_nwm":
            stride = round(args.context_period / 0.25)
            frames = history_images[-1 - 3 * stride :: stride]
            for attempt in range(3):
                if attempt:
                    frames = fresh_context(attempt)
                agent_response = agent_rpc(
                    {
                        "phase": "plan",
                        "request": {
                            "policy": "nwm",
                            "context": [str(out / f["path"]) for f in frames],
                            "timestamps": [f["stamp_s"] for f in frames],
                        },
                        "observation": live_observation(),
                    }
                )
                result["agent_plan_response"] = agent_response
                prediction_response = agent_response.get("prediction", {})
                result["learned_wam_invoked"] = prediction_response.get(
                    "learned_wam_invoked", False
                )
                result["decision"] = dict(
                    prediction_response, candidate=None, source="agent_proposal_not_yet_authorized"
                )
                result["reobservation_count"] = attempt
                if agent_response["status"] != "reobserve":
                    break
            if agent_response["status"] == "observe_original_route":
                plan = compute_detour_plan([[1.0, 0.0]], "continue-plan-before-agent.json")
                snapshot_deadline = time.monotonic() + 0.25
                while not live_observation()["robot_stationary"]:
                    tick()
                    if time.monotonic() >= snapshot_deadline:
                        raise RuntimeError("coherent_stationary_continue_observation_required")
                exposure = observe_clearance()
                verification = {
                    "observed_exposure": exposure,
                    "threshold": 0.06,
                    "passed": exposure <= 0.06,
                    "image": save_image("continue-observation.png"),
                }
                result["continue_verification"] = verification
                continuation = agent_rpc(
                    {
                        "phase": "observed_continue",
                        "verification": verification,
                        "observation": live_observation(),
                        "route_plan": plan,
                    }
                )
                result["continue_agent_response"] = continuation
                if continuation["status"] != "authorized_continue" or continuation.get(
                    "waypoints"
                ) != [[1.0, 0.0]]:
                    raise RuntimeError("observed_continue_not_authorized")
                decision = dict(
                    agent_response["prediction"],
                    candidate="direct",
                    source="observed_original_route_continue",
                )
                result["executed_wait_sim_s"] = 0.0
            else:
                if agent_response["status"] != "authorized_wait":
                    raise RuntimeError(
                        "agent_wait_not_authorized:"
                        + agent_response.get("reason", agent_response["status"])
                    )
                if state["t"] >= agent_response["target_at_sim_s"]:
                    raise RuntimeError("authorized_prediction_expired_before_executor")
                decision = dict(
                    agent_response["prediction"],
                    candidate="wait",
                    source="recovery_agent_after_assurance_and_policy",
                    wait_time_origin="agent_prediction_target",
                )
                result["learned_wam_invoked"] = decision["learned_wam_invoked"]
                result["wait_started_at_sim_s"] = state["t"]
        result["decision_latency_wall_s"] = time.monotonic() - inference_start
        result["decision_observation_age_sim_s"] = state["t"] - decision_observation_t
        result["decision"] = decision
        result["decision_input_sha256"] = hashlib.sha256(
            (out / "decision-input.json").read_bytes()
        ).hexdigest()
        (out / "decision.json").write_text(json.dumps(decision, indent=2))
        if decision["candidate"] is None:
            raise RuntimeError("no_candidate_passed_prediction_screen")
        detour = (
            {"wait_s": 0.0, "waypoints": [[0.35, -0.7], [1.0, 0.0]]}
            if args.compact_detour
            else CANDIDATES["detour"]
        )
        selected = (
            detour if decision["candidate"] == "detour" else CANDIDATES[decision["candidate"]]
        )
        if args.nav2_detour and decision["candidate"] == "detour":
            selected = CANDIDATES["direct"]
        result["persistent_obstacle_executor"] = (
            "Nav2_planner_avoidance"
            if args.nav2_detour
            else "compact_two_goal_detour"
            if args.compact_detour
            else "fixed_detour_waypoints"
        )
        decision_t = decision_observation_t
        episode_wall = inference_start
        bridge_results = []
        future_images = []
        next_future_image = decision_t + 0.25

        def bounded_tick():
            nonlocal next_future_image
            tick()
            if state["t"] >= next_future_image:
                future_images.append(save_image(f"observed-{len(future_images):03d}.png"))
                next_future_image = state["t"] + 0.25
                (out / "observed-images.json").write_text(json.dumps(future_images, indent=2))
            if state["t"] - decision_t > 65 or time.monotonic() - episode_wall > 150:
                raise RuntimeError("episode_budget_exhausted")

        wait_origin = (
            history_images[-1]["stamp_s"]
            if decision.get("wait_time_origin") == "last_context_timestamp"
            else decision_t
        )
        if args.policy == "agent_nwm" and decision["candidate"] == "wait":
            wait_origin = agent_response["target_at_sim_s"] - 4.0
        while state["t"] - wait_origin < selected["wait_s"]:
            bounded_tick()
        if decision["candidate"] == "wait":
            from wait_clearance import verify_clearance

            verification = verify_clearance(
                observe_clearance,
                bounded_tick,
                lambda: state["t"],
                wait_origin,
                decision.get("current_exposure", observe_clearance()),
            )
            if args.policy == "agent_nwm":
                result["executed_wait_sim_s"] = state["t"] - result["wait_started_at_sim_s"]
                if result["executed_wait_sim_s"] > 5.2:
                    raise RuntimeError("approved_wait_duration_exceeded")
            verification["image"] = save_image("post-wait.png")
            result["post_wait_verification"] = verification
            if args.policy == "agent_nwm":
                if not verification["passed"]:
                    if not args.approve_bounded_detour:
                        raise RuntimeError("post_wait_actual_clearance_failed")
                    result["initial_failed_wait_verification"] = verification
                    plan = compute_detour_plan(detour["waypoints"], "detour-plan-before-agent.json")
                    exposure_now = observe_clearance()
                    verification = dict(
                        verification,
                        observed_exposure=exposure_now,
                        passed=exposure_now <= 0.06,
                        image=save_image("post-planning-observation.png"),
                        reobserved_after_route_planning=True,
                    )
                    result["post_wait_verification"] = verification
                    if not verification["passed"]:
                        detour_response = agent_rpc(
                            {
                                "phase": "failed_wait",
                                "verification": verification,
                                "observation": live_observation(),
                                "route_plan": plan,
                            }
                        )
                        result["detour_agent_response"] = detour_response
                        if (
                            detour_response["status"] != "authorized_detour"
                            or detour_response.get("waypoints") != detour["waypoints"]
                        ):
                            raise RuntimeError("post_wait_detour_not_authorized")
                if verification["passed"]:
                    post = agent_rpc(
                        {
                            "phase": "post_wait",
                            "verification": verification,
                            "observation": live_observation(),
                        }
                    )
                    result["post_wait_agent_response"] = post
                    if post["status"] != "continue":
                        raise RuntimeError("post_wait_assurance_did_not_allow_continue")
                    if observe_clearance() > 0.06 or not live_observation()["robot_stationary"]:
                        raise RuntimeError("clearance_changed_during_post_wait_assurance")
            observed_exposure = verification["observed_exposure"]
            if observed_exposure > 0.06:
                selected = CANDIDATES["direct"] if args.nav2_detour else detour
                result["executed_candidate"] = (
                    "agent_detour_after_wait_observation"
                    if args.policy == "agent_nwm"
                    else "detour_after_wait_observation"
                )
            else:
                result["executed_candidate"] = "wait_then_direct"
        else:
            result["executed_candidate"] = (
                "observed_original_route_continue"
                if decision.get("source") == "observed_original_route_continue"
                else decision["candidate"]
            )
        if args.refresh_costmaps:
            from nav2_msgs.srv import ClearEntireCostmap

            result["pre_dispatch_costmap_refresh"] = []
            for service in (
                "/local_costmap/clear_entirely_local_costmap",
                "/global_costmap/clear_entirely_global_costmap",
            ):
                client = node.create_client(ClearEntireCostmap, service)
                wait_for(client.service_is_ready, 5, "costmap_refresh_service_unavailable")
                future = client.call_async(ClearEntireCostmap.Request())
                wait_for(future.done, 5, "costmap_refresh_timeout")
                if future.result() is None:
                    raise RuntimeError("costmap_refresh_failed")
                result["pre_dispatch_costmap_refresh"].append(
                    {"service": service, "response_received": True}
                )
            refresh_t = state["t"]
            while state["t"] - refresh_t < 0.5:
                bounded_tick()
        if args.policy == "agent_nwm":
            using_detour = result.get("executed_candidate") == "agent_detour_after_wait_observation"
            using_continue = result.get("executed_candidate") == "observed_original_route_continue"
            if using_continue:
                result["pre_dispatch_continue_plan"] = compute_detour_plan(
                    [[1.0, 0.0]], "continue-plan-before-dispatch.json"
                )
            if using_detour:
                result["pre_dispatch_detour_plan"] = compute_detour_plan(
                    detour["waypoints"], "detour-plan-before-dispatch.json"
                )
            if using_continue:
                snapshot_deadline = time.monotonic() + 0.25
                while not live_observation()["robot_stationary"]:
                    bounded_tick()
                    if time.monotonic() >= snapshot_deadline:
                        raise RuntimeError("coherent_stationary_dispatch_observation_required")
            if (
                not live_observation()["robot_stationary"]
                or (not using_detour and observe_clearance() > 0.06)
                or (using_detour and state["t"] >= detour_response["dispatch_before_sim_s"])
                or (using_continue and state["t"] >= continuation["dispatch_before_sim_s"])
            ):
                raise RuntimeError("fresh_pre_dispatch_observation_failed")
            result["pre_dispatch_observation"] = dict(
                live_observation(),
                exposure=observe_clearance(),
                image=save_image("pre-dispatch.png"),
            )
            result["agent_chain_completed_before_dispatch"] = True
        env = dict(
            os.environ,
            RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE="1",
            ROS2_NAV2_INITIALPOSE_ENABLE="0",
            ROS2_NAV2_USE_SIM_TIME="1",
            ROS2_NAV2_GOAL_RESULT_TIMEOUT_S="140",
            ROS2_NAV2_ACTION_SERVER_TIMEOUT_S="8",
            ROS2_NAV2_POST_RESULT_SETTLE_S=".5",
            ROS2_NAV2_TRAJECTORY_OBSERVE_ENABLE="1",
            ROS2_NAV2_VELOCITY_OBSERVE_ENABLE="1",
            ROS2_NAV2_TF_READY_TIMEOUT_S="10",
            ROS2_NAV2_REQUIRE_TF_READY="1",
        )
        result["via_heading"] = args.via_heading
        result["goal_stages"] = []
        for i, (x, y) in enumerate(selected["waypoints"]):
            if abs(x) > 1.5 or abs(y) > 1.0:
                raise RuntimeError("waypoint_outside_approved_benchmark_bounds")
            stage = dict(
                goal_index=i,
                start_sim_s=state["t"] - t0,
                robot_xy_m=state["robot"][:],
                cart_xy_m=state["cart"][:],
            )
            result["goal_stages"].append(stage)
            yaw = 0.0
            if args.compact_detour and i + 1 < len(selected["waypoints"]):
                nx, ny = selected["waypoints"][i + 1]
                yaw = (
                    math.atan2(y - stage["robot_xy_m"][1], x - stage["robot_xy_m"][0])
                    if args.via_heading == "incoming"
                    else math.atan2(ny - y, nx - x)
                )
            stage["requested_yaw_rad"] = yaw
            stage["heading_basis"] = (
                args.via_heading if i + 1 < len(selected["waypoints"]) else "original_goal_heading"
            )
            request = {
                "action": "send_goal_pose",
                "raw_velocity_allowed": False,
                "raw_ros_topic_publication_allowed": False,
                "physical_execution_invoked": False,
                "payload": {
                    "frame_id": "map",
                    "x_m": x,
                    "y_m": y,
                    "yaw_rad": yaw,
                    "tolerance_m": 0.30,
                    "max_speed_mps": 0.20,
                    "max_distance_m": 5.0,
                    "label": "tb3_prediction_benchmark",
                },
            }
            (out / f"goal-{i}-request.json").write_text(json.dumps(request, indent=2))
            with (
                (out / f"goal-{i}.json").open("w") as stdout,
                (out / f"goal-{i}.stderr").open("w") as stderr,
            ):
                process = subprocess.Popen(
                    ["/usr/bin/python3", "scripts/ros2_nav2_turtlebot3_bridge.py"],
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                )
                processes.append(process)
                process.stdin.write(json.dumps(request).encode())
                process.stdin.close()
                while process.poll() is None:
                    bounded_tick()
            bridge_result = json.loads((out / f"goal-{i}.json").read_text())
            stage.update(
                end_sim_s=state["t"] - t0,
                nav2_status=bridge_result.get("nav2_status"),
                end_robot_xy_m=state["robot"][:],
            )
            bridge_results.append(bridge_result)
            if bridge_result.get("nav2_status") != "succeeded":
                raise RuntimeError("nav2_goal_did_not_succeed")
        result["status"] = "completed"
        result["bridge_goals_succeeded"] = len(bridge_results)
    except Exception as error:
        result["status"] = "blocked_or_incomplete"
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        # On failure cancel through the production bridge before ending the container.
        active = [p for p in processes if p.poll() is None]
        if active:
            request = dict(
                action="cancel_goal",
                payload={},
                raw_velocity_allowed=False,
                raw_ros_topic_publication_allowed=False,
                physical_execution_invoked=False,
            )
            try:
                cancel = subprocess.run(
                    ["/usr/bin/python3", "scripts/ros2_nav2_turtlebot3_bridge.py"],
                    input=json.dumps(request),
                    text=True,
                    capture_output=True,
                    env=dict(os.environ, RUN_MISSIONOS_ROS2_NAV2_TURTLEBOT3_BRIDGE="1"),
                    timeout=20,
                )
                (out / "cancel.json").write_text(cancel.stdout)
            except subprocess.TimeoutExpired:
                result["cancel_status"] = "timeout_container_removal_required"
            for process in active:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if samples:
            final = samples[-1]
            result.update(
                elapsed_sim_s=final["t_s"],
                final_robot_xy_m=final["robot_xy_m"],
                goal_distance_m=math.dist(final["robot_xy_m"], [1, 0]),
                observed_path_length_m=sum(
                    math.dist(a["robot_xy_m"], b["robot_xy_m"])
                    for a, b in zip(samples, samples[1:])
                ),
                min_cart_center_distance_m=min(
                    math.dist(s["robot_xy_m"], s["cart_xy_m"]) for s in samples
                ),
                stationary_sim_s=sum(
                    b["t_s"] - a["t_s"]
                    for a, b in zip(samples, samples[1:])
                    if math.dist(a["robot_xy_m"], b["robot_xy_m"]) < 0.002
                ),
            )
            result["goal_reached_observed"] = result["goal_distance_m"] <= 0.30
            result["robot_motion_observed"] = result["observed_path_length_m"] > 0.1
        if "inference_start" in locals():
            result["elapsed_decision_to_end_wall_s"] = time.monotonic() - inference_start
            result["decision_to_end_sim_s"] = state["t"] - decision_observation_t
        result["contact_message_count"] = state["contact_messages"]
        result["cart_contact_event_count"] = (
            len(state["contact_events"]) if state["contact_messages"] else None
        )
        result["navigation_completion_verified"] = (
            result["status"] == "completed"
            and result.get("goal_reached_observed", False)
            and result.get("robot_motion_observed", False)
        )
        result["actor_pose_apply_receipts"] = actor_receipts
        for name, data in [
            ("trajectory.json", samples),
            ("contacts.json", contacts),
            ("result.json", result),
        ]:
            (out / name).write_text(json.dumps(data, indent=2) + "\n")
        node.destroy_node()
        rclpy.shutdown()
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("status", "scenario", "policy", "navigation_completion_verified")
            }
        )
    )
    return 0 if result["navigation_completion_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

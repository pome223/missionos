"""Go2 rigid-body simulation with an external, pinned RL locomotion policy.

MissionOS sends destinations. A* and a heading controller run here; the learned
policy runs at 50 Hz and torque PD at 200 Hz. Only initialisation writes robot
qpos. Every subsequent robot displacement is produced by MuJoCo integration.
Navigation uses known geometry and simulator pose (no SLAM/perception claim).
"""

from __future__ import annotations

import heapq
import math
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np

from simulators.go2_delivery.dynamic_obstacles import (
    CLEAR_HOLD_S,
    MAX_YIELD_S,
    ROBOT_RADIUS_M,
    velocity_guard,
)

OBSTACLES = [
    (-4.0, 0.0, 0.1, 3.0),
    (4.0, 0.0, 0.1, 3.0),
    (0.0, -3.0, 4.0, 0.1),
    (0.0, 3.0, 4.0, 0.1),
    (0.0, 0.0, 0.09, 1.0),
    (2.5, 1.9, 0.6, 0.35),
]


def wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


class OfficePlanner:
    resolution = 0.1
    clearance = 0.45

    def __init__(self):
        self.obstacles = list(OBSTACLES)

    def free(self, point):
        x, y = point
        return (
            abs(x) <= 3.5
            and abs(y) <= 2.5
            and all(
                abs(x - ox) > sx + self.clearance or abs(y - oy) > sy + self.clearance
                for ox, oy, sx, sy in self.obstacles
            )
        )

    def visible(self, a, b):
        return all(self.free(p) for p in np.linspace(a, b, max(2, int(math.dist(a, b) / 0.04) + 1)))

    def path(self, start, goal):
        def cell(p):
            return tuple(round(v / self.resolution) for v in p)

        def point(p):
            return tuple(v * self.resolution for v in p)

        begin, end = cell(start), cell(goal)
        if not self.free(start) or not self.free(goal):
            return []
        queue, costs, parent = [(0.0, begin)], {begin: 0.0}, {}
        while queue:
            _, current = heapq.heappop(queue)
            if current == end:
                cells = [current]
                while current != begin:
                    current = parent[current]
                    cells.append(current)
                raw = [tuple(start)] + [point(c) for c in reversed(cells)] + [tuple(goal)]
                smooth, i = [raw[0]], 0
                while i < len(raw) - 1:
                    j = len(raw) - 1
                    while j > i + 1 and not self.visible(raw[i], raw[j]):
                        j -= 1
                    smooth.append(raw[j])
                    i = j
                return smooth
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                neighbor = current[0] + dx, current[1] + dy
                if not self.free(point(neighbor)):
                    continue
                # Do not cut across a blocked corner on diagonal steps.
                if (
                    dx
                    and dy
                    and (
                        not self.free(point((current[0] + dx, current[1])))
                        or not self.free(point((current[0], current[1] + dy)))
                    )
                ):
                    continue
                cost = costs[current] + math.hypot(dx, dy)
                if cost < costs.get(neighbor, float("inf")):
                    costs[neighbor], parent[neighbor] = cost, current
                    heapq.heappush(queue, (cost + math.dist(neighbor, end), neighbor))
        return []


class Go2Physics:
    def __init__(self, model_path: Path, policy_path: Path):
        if os.getenv("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
            raise RuntimeError("Go2 simulator requires explicit opt-in")
        import mujoco
        import torch

        self.mj, self.torch = mujoco, torch
        root = ET.parse(model_path).getroot()
        root.find("compiler").set("meshdir", str((model_path.parent / "assets").resolve()))
        root.find("option").set("timestep", ".005")
        ET.SubElement(
            root.find(".//body[@name='base_link']"),
            "camera",
            name="go2_front_wam",
            pos=".30 0 .15",
            xyaxes="0 -1 0 0 0 1",
            fovy="60",
        )
        visual = ET.SubElement(root, "visual")
        ET.SubElement(visual, "global", offwidth="960", offheight="640")
        ET.SubElement(visual, "headlight", ambient=".15 .15 .15", diffuse=".35 .35 .35")
        world = root.find("worldbody")
        ET.SubElement(
            world, "light", pos="0 0 5", dir="0 0 -1", directional="true", diffuse=".5 .5 .5"
        )
        ET.SubElement(
            world, "geom", name="floor", type="plane", size="0 0 .1", rgba=".52 .58 .65 1"
        )
        for i, (x, y, sx, sy) in enumerate(OBSTACLES):
            height = 0.6 if i != 5 else 0.7
            ET.SubElement(
                world,
                "geom",
                name=f"office_{i}",
                type="box",
                pos=f"{x} {y} {height / 2}",
                size=f"{sx} {sy} {height / 2}",
                rgba=".31 .39 .49 1" if i == 4 else ".65 .69 .74 1",
            )
        for name, x, color in (
            ("reception", -2.5, ".2 .65 .85 .6"),
            ("meeting_room_a", 2.5, ".25 .75 .5 .6"),
        ):
            ET.SubElement(
                world,
                "geom",
                name=name,
                type="cylinder",
                pos=f"{x} 0 .004",
                size=".4 .004",
                rgba=color,
                contype="0",
                conaffinity="0",
            )
        for sign, name in ((-1, "south"), (1, "north")):
            cart = ET.SubElement(
                world, "body", name=f"cart_{name}", mocap="true", pos=f"0 {2 * sign} -2"
            )
            ET.SubElement(
                cart,
                "geom",
                name=f"cart_geom_{name}",
                type="box",
                size=".16 1 .35",
                rgba=".95 .46 .15 1",
            )
        moving = ET.SubElement(
            world, "body", name="moving_target", mocap="true", pos="-1.25 1.25 -2"
        )
        ET.SubElement(
            moving,
            "geom",
            name="moving_geom_target",
            type="cylinder",
            size=".24 .45",
            rgba="1 .25 .13 1",
        )
        self.model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
        self.data = mujoco.MjData(self.model)
        self.body = self.model.body("base_link").id
        joints = self.model.actuator_trnid[:, 0]
        self.qadr, self.vadr = self.model.jnt_qposadr[joints], self.model.jnt_dofadr[joints]
        self.default, self.scale = np.tile([0.0, 0.8, -1.5], 4), np.tile([0.125, 0.25, 0.25], 4)
        self.data.qpos[:3] = [-2.5, 0.0, 0.33]
        self.data.qpos[self.qadr] = self.default
        mujoco.mj_forward(self.model, self.data)
        torch.set_num_threads(1)
        self.policy = torch.jit.load(str(policy_path), map_location="cpu").eval()
        self.actions, self.target = np.zeros(12), self.default.copy()
        self.ticks, self.policy_calls = 0, 0
        self.last_step_wall = time.monotonic()
        self.moving_enabled = False

    def enable_moving_obstacle(self):
        self.moving_enabled = True
        self._move_obstacle(0.0)
        self.mj.mj_forward(self.model, self.data)

    def _move_obstacle(self, sim_time):
        # Only the disturbance harness knows this trajectory. Navigation sees
        # positions read back from MuJoCo and finite-difference velocity only.
        mocap = self.model.body("moving_target").mocapid[0]
        y = max(-2.6, 1.25 - 0.2 * max(0.0, sim_time - 3.0))
        self.data.mocap_pos[mocap] = [-1.25, y, 0.5]

    def moving_position(self):
        return self.data.geom_xpos[self.model.geom("moving_geom_target").id, :2].tolist()

    def step(self, command):
        if self.moving_enabled:
            self._move_obstacle(self.data.time + self.model.opt.timestep)
        if self.ticks % 4 == 0:
            rot = self.data.xmat[self.body].reshape(3, 3)
            obs = np.concatenate(
                [
                    self.data.sensor("imu_gyro").data * 0.25,
                    rot.T @ np.array([0.0, 0.0, -1.0]),
                    command,
                    self.data.qpos[self.qadr] - self.default,
                    self.data.qvel[self.vadr] * 0.05,
                    self.actions,
                ]
            ).astype(np.float32)
            with self.torch.inference_mode():
                self.actions = (
                    self.policy(self.torch.from_numpy(obs[None].clip(-100, 100)))
                    .numpy()[0]
                    .clip(-100, 100)
                )
            if self.actions.shape != (12,) or not np.isfinite(self.actions).all():
                raise RuntimeError("invalid locomotion policy output")
            self.target = self.default + self.scale * self.actions
            self.policy_calls += 1
        self.data.ctrl[:] = np.clip(
            20 * (self.target - self.data.qpos[self.qadr]) - 0.5 * self.data.qvel[self.vadr],
            -23.5,
            23.5,
        )
        self.mj.mj_step(self.model, self.data)
        self.ticks += 1
        self.last_step_wall = time.monotonic()

    def state(self):
        w, x, y, z = self.data.qpos[3:7]
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        pitch = math.asin(np.clip(2 * (w * y - z * x), -1.0, 1.0))
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        px, py, height = self.data.qpos[:3]
        contacts = []
        for c in self.data.contact:
            names = [
                self.mj.mj_id2name(self.model, self.mj.mjtObj.mjOBJ_GEOM, int(g)) or ""
                for g in (c.geom1, c.geom2)
            ]
            if any(n.startswith(("office_", "cart_geom_", "moving_geom_")) for n in names):
                contacts.append(names)
        return dict(
            sim_time_s=float(self.data.time),
            ground_truth_xy=[float(px), float(py)],
            height_m=float(height),
            roll_rad=roll,
            pitch_rad=pitch,
            yaw_rad=yaw,
            measured_speed_mps=float(np.linalg.norm(self.data.qvel[:2])),
            measured_velocity_xy_mps=self.data.qvel[:2].tolist(),
            base_stable=bool(0.18 < height < 0.6 and abs(roll) < 0.65 and abs(pitch) < 0.65),
            geofence_satisfied=bool(abs(px) < 3.6 and abs(py) < 2.6),
            telemetry_fresh=time.monotonic() - self.last_step_wall < 3.0,
            heartbeat_alive=True,
            state_observed=True,
            obstacle_contacts=contacts,
            physics_steps=self.ticks,
            policy_inference_calls=self.policy_calls,
        )

    def block(self, sign):
        name = "south" if sign < 0 else "north"
        mocap = self.model.body(f"cart_{name}").mocapid[0]
        self.data.mocap_pos[mocap] = [0.0, 2.0 * sign, 0.35]
        self.mj.mj_forward(self.model, self.data)

    def unblock(self, sign):
        name = "south" if sign < 0 else "north"
        mocap = self.model.body(f"cart_{name}").mocapid[0]
        self.data.mocap_pos[mocap] = [0.0, 2.0 * sign, -2.0]
        self.mj.mj_forward(self.model, self.data)


class MuJoCoDeliveryClient:
    metadata = dict(
        simulator="MuJoCo",
        navigation="known_map_astar_and_heading_feedback",
        actuator_model="learned_position_targets_with_torque_limited_pd",
        ground_truth_used_by_navigation=True,
        obstacles_from="simulator_geometry",
        robot_pose_overwrites_after_initialization=False,
        real_unitree_sdk_invoked=False,
        ros2_nav2_invoked=False,
    )

    def __init__(self, physics, *, emit, observe, scenario="baseline", timeout=180.0):
        self.physics, self.emit, self.observe = physics, emit, observe
        self.scenario, self.timeout = scenario, timeout
        self.planner = OfficePlanner()
        self.status, self.moved, self.phase = "idle", False, "Preparing"
        self.route, self.visited = [], []
        self.injected = False
        self.last_observation = -1.0
        self.stop_yaw = 0.0
        self.safety_violation = False
        self.cancel_requested = lambda: False
        self.map_revision = 0
        self.release_at = None
        self.notice_until = None
        self.blocked_sides = []
        self.dynamic_track = None
        self.dynamic_yield = None
        self.dynamic_clear_since = None
        self.dynamic_events = []
        self.dynamic_contact_steps = 0
        self.dynamic_min_separation = None
        self.dynamic_travel = 0.0
        self.dynamic_yield_count = 0
        if scenario == "moving_obstacle":
            physics.enable_moving_obstacle()
            self._observe_dynamic()

    def _observe_dynamic(self):
        now = float(self.physics.data.time)
        previous = self.dynamic_track
        if previous is not None and now - previous["observed_sim_time_s"] < 0.05 - 1e-7:
            return
        xy = self.physics.moving_position()
        dt = now - previous["observed_sim_time_s"] if previous else 0.0
        if previous:
            self.dynamic_travel += math.dist(xy, previous["xy"])
        self.dynamic_track = dict(
            obstacle_id="moving_target",
            source="simulator_geometry_readback",
            xy=xy,
            radius_m=0.24,
            observed_sim_time_s=now,
            velocity_observed=dt > 0,
            velocity_xy_mps=[(p - q) / dt for p, q in zip(xy, previous["xy"])]
            if dt > 0
            else [0.0, 0.0],
        )

    def _dynamic_event(self, event, **fields):
        item = dict(
            event=event,
            sim_time_s=float(self.physics.data.time),
            source="local_navigation_guard",
            **fields,
        )
        self.dynamic_events.append(item)
        self.emit(item)

    def dynamic_summary(self):
        separation = self.dynamic_min_separation
        return dict(
            enabled=self.scenario == "moving_obstacle",
            source="simulator_geometry_readback",
            controller="local_velocity_guard_yield_and_resume",
            obstacle_travel_m=self.dynamic_travel,
            minimum_center_separation_m=separation,
            minimum_conservative_clearance_m=separation - ROBOT_RADIUS_M - 0.24
            if separation is not None
            else None,
            robot_envelope_radius_m=ROBOT_RADIUS_M,
            obstacle_radius_m=0.24,
            contact_physics_steps=self.dynamic_contact_steps,
            yield_count=self.dynamic_yield_count,
            events=self.dynamic_events,
        )

    def read_state(self):
        return dict(
            self.physics.state(),
            navigation_status=self.status,
            robot_motion_observed=self.moved,
            safety_violation_observed=self.safety_violation,
            operator_cancel_requested=self.cancel_requested(),
            map_revision=self.map_revision,
            moving_obstacle=self.dynamic_track,
            local_avoidance_state="yielding" if self.dynamic_yield is not None else "clear",
        )

    def supervision_state(self, plan, leg):
        state = self.read_state()
        xy = state["ground_truth_xy"]
        target = plan.destination_xy if leg == "outbound" else plan.home_xy
        route = self.planner.path(xy, target)
        home = self.planner.path(xy, plan.home_xy)
        return dict(
            state,
            delivery_route_available=bool(route),
            home_route_available=bool(home),
            delivery_route_length_m=sum(math.dist(a, b) for a, b in zip(route, route[1:])),
            home_route_length_m=sum(math.dist(a, b) for a, b in zip(home, home[1:])),
            closure_notice={
                "source": "simulated_facility_notice",
                "estimate_only": True,
                "expected_clear_in_sim_s": max(0.0, self.notice_until - state["sim_time_s"])
                if self.notice_until is not None
                else None,
            },
        )

    def tick(self, command):
        self.physics.step(command)
        if self.scenario == "moving_obstacle":
            self._observe_dynamic()
        if self.release_at is not None and self.physics.data.time >= self.release_at:
            for side in self.blocked_sides:
                self.physics.unblock(side)
                self.planner.obstacles.remove((0.0, 2.0 * side, 0.16, 1.0))
            self.release_at = None
            self.map_revision += 1
            self.emit(
                dict(
                    event="passage_reopened",
                    source="simulation_scenario_harness",
                    observed_in_physics=True,
                    map_revision=self.map_revision,
                )
            )
        state = self.read_state()
        if self.scenario == "moving_obstacle":
            separation = math.dist(state["ground_truth_xy"], self.physics.moving_position())
            self.dynamic_min_separation = min(
                self.dynamic_min_separation or float("inf"), separation
            )
            self.dynamic_contact_steps += bool(
                any(
                    any(n.startswith("moving_geom_") for n in pair)
                    for pair in state["obstacle_contacts"]
                )
            )
            if self.dynamic_yield is not None:
                hold = self.dynamic_yield
                hold["travel_after_stop_request_m"] += math.dist(
                    state["ground_truth_xy"], hold["last_xy"]
                )
                hold["last_xy"] = state["ground_truth_xy"]
                if state["measured_speed_mps"] < 0.06:
                    if hold["slow_since"] is None:
                        hold["slow_since"] = state["sim_time_s"]
                    if (
                        not hold["stop_observed"]
                        and state["sim_time_s"] - hold["slow_since"] >= 0.3
                    ):
                        hold["stop_observed"] = True
                        self._dynamic_event(
                            "dynamic_stop_observed",
                            measured_speed_mps=state["measured_speed_mps"],
                            since_request_s=state["sim_time_s"] - hold["started"],
                            travel_after_stop_request_m=hold["travel_after_stop_request_m"],
                        )
                else:
                    hold["slow_since"] = None
        if (
            not state["base_stable"]
            or not state["geofence_satisfied"]
            or state["obstacle_contacts"]
        ):
            self.safety_violation = True
        if state["sim_time_s"] - self.last_observation >= 0.1 - 1e-7:
            self.last_observation = state["sim_time_s"]
            self.visited.append(state["ground_truth_xy"])
            self.observe(state, self)
        return state

    def wait(self, seconds):
        until = self.physics.data.time + seconds
        while self.physics.data.time < until:
            state = self.read_state()
            # Feedback holds heading during idle; zero planar velocity request.
            self.tick(
                [0.0, 0.0, float(np.clip(1.6 * wrap(self.stop_yaw - state["yaw_rad"]), -0.5, 0.5))]
            )

    def send_goal_pose(self, goal):
        self.phase = "Outbound" if goal.label == "outbound" else "Returning"
        self.status, self.moved = "active", False
        initial = self.read_state()["ground_truth_xy"]
        target = [goal.x_m, goal.y_m]
        self.route = self.planner.path(initial, target)
        self.emit(dict(event="goal_accepted", goal_xy=target, source="mujoco_goal_executor"))
        self.emit(dict(event="path_planned", path=self.route, source="known_map_astar"))
        if not self.route:
            self.status = "aborted"
            self.emit(dict(event="navigation_aborted", reason="no_clear_route"))
            return dict(ack_status="accepted", ack_source="mujoco_goal_executor")
        started = self.physics.data.time
        deadline = time.monotonic() + 120
        index = 1
        arrival_started = False
        while self.physics.data.time - started < self.timeout and time.monotonic() < deadline:
            state = self.read_state()
            xy = state["ground_truth_xy"]
            self.moved |= math.dist(xy, initial) > 0.1
            if self.cancel_requested():
                self.status = "canceled"
                self.emit(dict(event="navigation_canceled", reason="operator_cancel"))
                break
            if (
                not state["base_stable"]
                or not state["geofence_satisfied"]
                or state["obstacle_contacts"]
                or state["safety_violation_observed"]
            ):
                self.status = "canceled"
                self.emit(
                    dict(
                        event="navigation_canceled", reason="operating_state_violation", state=state
                    )
                )
                break
            if (
                self.scenario in ("blocked_passage", "all_blocked", "temporary_blockage")
                and not self.injected
                and goal.label == "outbound"
                and self.physics.data.time - started > 6
            ):
                crossing = min(self.route, key=lambda p: abs(p[0]))
                sign = -1 if crossing[1] < 0 else 1
                signs = (
                    [sign, -sign]
                    if self.scenario in ("all_blocked", "temporary_blockage")
                    else [sign]
                )
                for side in signs:
                    self.physics.block(side)
                    self.planner.obstacles.append((0.0, 2.0 * side, 0.16, 1.0))
                self.injected = True
                self.map_revision += 1
                self.blocked_sides = signs
                if self.scenario == "temporary_blockage":
                    self.release_at = float(self.physics.data.time) + 12.0
                    self.notice_until = float(self.physics.data.time) + 12.0
                self.emit(
                    dict(
                        event="passage_blocked",
                        passages=signs,
                        source="simulation_scenario_harness",
                        observed_in_physics=True,
                    )
                )
                self.status = "aborted"
                self.phase = "Waiting: route blocked"
                self.stop_yaw = state["yaw_rad"]
                self.emit(dict(event="navigation_aborted", reason="planned_passage_blocked"))
                break
            distance = math.dist(xy, target)
            intent_velocity = [0.0, 0.0]
            # Leave room for gait settling after the zero-velocity request.
            # Completion still checks the original, unchanged goal tolerance.
            if distance < 0.7 * goal.tolerance_m:
                arrival_started = True
            elif distance >= goal.tolerance_m:
                arrival_started = False
            if arrival_started:
                self.stop_yaw = goal.yaw_rad
                if abs(wrap(goal.yaw_rad - state["yaw_rad"])) < 0.15:
                    self.wait(2.0)
                    settled = self.read_state()
                    # Preserve the observed stop cause even when the hold has
                    # crossed the loop deadline. Match cancellation precedence
                    # at the loop entry before considering correction or success.
                    if self.cancel_requested():
                        self.status = "canceled"
                        self.emit(dict(event="navigation_canceled", reason="operator_cancel"))
                        break
                    if (
                        not settled["base_stable"]
                        or not settled["geofence_satisfied"]
                        or settled["obstacle_contacts"]
                        or settled["safety_violation_observed"]
                    ):
                        self.status = "canceled"
                        self.emit(
                            dict(
                                event="navigation_canceled",
                                reason="operating_state_violation",
                                state=settled,
                            )
                        )
                        break
                    if math.dist(settled["ground_truth_xy"], target) <= goal.tolerance_m:
                        self.status = "succeeded"
                        break
                    # Correct settling drift within the same goal and original
                    # navigation deadline; the guard is checked again each tick.
                    arrival_started = False
                    continue
                command = [
                    0.0,
                    0.0,
                    float(np.clip(1.6 * wrap(goal.yaw_rad - state["yaw_rad"]), -0.5, 0.5)),
                ]
            else:
                while index < len(self.route) - 1 and math.dist(xy, self.route[index]) < 0.22:
                    index += 1
                waypoint = self.route[index]
                heading = math.atan2(waypoint[1] - xy[1], waypoint[0] - xy[0])
                error = wrap(heading - state["yaw_rad"])
                # Keep checking the intended path while holding or turning;
                # a zero current command alone cannot establish safe resumption.
                intent_speed = min(goal.max_speed_mps, 0.6 * distance)
                intent_velocity = [
                    intent_speed * math.cos(heading),
                    intent_speed * math.sin(heading),
                ]
                speed = min(goal.max_speed_mps, 0.6 * distance) * max(0.0, math.cos(error))
                if abs(error) > 0.65:
                    speed = 0.0
                command = [speed, 0.0, float(np.clip(1.8 * error, -0.6, 0.6))]
            if self.scenario == "moving_obstacle":
                guard = velocity_guard(state, command, self.dynamic_track, intent_velocity)
                if guard["hold"]:
                    self.dynamic_clear_since = None
                    if self.dynamic_yield is None:
                        self.dynamic_yield_count += 1
                        self.dynamic_yield = dict(
                            started=state["sim_time_s"],
                            yaw=state["yaw_rad"],
                            last_xy=xy,
                            travel_after_stop_request_m=0.0,
                            slow_since=None,
                            stop_observed=False,
                        )
                        self._dynamic_event(
                            "dynamic_yield_requested", observation=self.dynamic_track, guard=guard
                        )
                elif self.dynamic_yield is not None:
                    if self.dynamic_clear_since is None:
                        self.dynamic_clear_since = state["sim_time_s"]
                    if (
                        state["sim_time_s"] - self.dynamic_clear_since >= CLEAR_HOLD_S
                        and self.dynamic_yield["stop_observed"]
                    ):
                        self._dynamic_event(
                            "dynamic_path_clear_observed",
                            waited_sim_s=state["sim_time_s"] - self.dynamic_yield["started"],
                            observation=self.dynamic_track,
                            guard=guard,
                        )
                        self.dynamic_yield = None
                        self.phase = "Outbound" if goal.label == "outbound" else "Returning"
                if self.dynamic_yield is not None:
                    self.phase = "Yielding to moving obstacle"
                    if state["sim_time_s"] - self.dynamic_yield["started"] >= MAX_YIELD_S:
                        # A prolonged obstruction becomes a mission exception.
                        # Never silently drive through it or reset the budget.
                        self.status = "aborted"
                        self._dynamic_event("dynamic_yield_budget_exhausted")
                        break
                    command = [
                        0.0,
                        0.0,
                        float(
                            np.clip(
                                1.6 * wrap(self.dynamic_yield["yaw"] - state["yaw_rad"]), -0.5, 0.5
                            )
                        ),
                    ]
            self.tick(command)
        else:
            self.status = "aborted"
            self.emit(dict(event="navigation_aborted", reason="navigation_timeout"))
        self.stop_yaw = self.read_state()["yaw_rad"] if self.status != "succeeded" else goal.yaw_rad
        if self.status != "succeeded":
            self.wait(2.0)
        final = self.read_state()
        if self.status == "succeeded" and (
            math.dist(final["ground_truth_xy"], target) > goal.tolerance_m
            or not final["base_stable"]
        ):
            self.status = "aborted"
        self.emit(
            dict(event="navigation_result", status=self.status, final_state=self.read_state())
        )
        return dict(ack_status="accepted", ack_source="mujoco_goal_executor")

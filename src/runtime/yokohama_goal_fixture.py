"""CPU kinematic fixture executor and a separate observation verifier.

The executor follows approved coordinates, the receiver observes the dropped
fixture payload at the selected ground point, and return checks observed home.
This is an executable fixture, never evidence of physical flight or Gazebo.
"""

import math

from src.runtime.yokohama_payload import digest


def samples(points, step=10):
    yield points[0]
    for a, b in zip(points, points[1:]):
        count = max(1, math.ceil(math.dist(a, b) / step))
        for i in range(1, count + 1):
            yield [a[j] + (b[j] - a[j]) * i / count for j in range(3)]


def verify(plan, observations):
    poses = [o for o in observations if o["kind"] == "pose"]
    payloads = [o for o in observations if o["kind"] == "payload_pose"]
    binding = digest(plan)
    valid_binding = all(o.get("plan_sha256") == binding for o in observations)
    goal = plan["goal_source_xyz_m"]
    delivery = any(
        o["phase"] == "outbound" and math.dist(o["xyz_m"], [*goal[:2], goal[2] + 3]) < 0.05
        for o in poses
    )
    received = len(payloads) >= 3 and all(math.dist(o["xyz_m"], goal) < 0.05 for o in payloads[-3:])
    returned = bool(
        poses
        and poses[-1]["phase"] == "return"
        and math.dist(poses[-1]["xyz_m"], plan["home_source_xyz_m"]) < 0.05
    )
    return dict(
        passed=valid_binding and delivery and received and returned,
        plan_sha256=binding,
        goal_reached=delivery,
        payload_received=received,
        home_returned=returned,
        observation_count=len(observations),
        execution_target="cpu_kinematic_fixture",
        physical_execution_invoked=False,
    )


def run(plan, emit, canceled, delay=0.01):
    observations = []
    binding = digest(plan)

    def observe(kind, phase, xyz):
        if canceled.wait(delay):
            return False
        row = dict(
            sequence=len(observations), kind=kind, phase=phase, xyz_m=list(xyz), plan_sha256=binding
        )
        observations.append(row)
        emit(row)
        return True

    for phase, key in [("outbound", "outbound_source_xyz_m"), ("return", "return_source_xyz_m")]:
        for xyz in samples(plan[key]):
            if not observe("pose", phase, xyz):
                return dict(passed=False, canceled=True, physical_execution_invoked=False)
        if phase == "outbound":
            for _ in range(3):
                if not observe("payload_pose", "receive", plan["goal_source_xyz_m"]):
                    return dict(passed=False, canceled=True, physical_execution_invoked=False)
    return verify(plan, observations)

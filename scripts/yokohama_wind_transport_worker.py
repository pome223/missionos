"""Gazebo-only transport smoke: teleport a marker, not a flight or AP trial."""

import json
import time
from pathlib import Path
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.boolean_pb2 import Boolean
from yokohama_sitl_worker import Observer
from yokohama_wind_profile import requested_wind

ROOT = Path("/mission")
c = json.loads((ROOT / "config.json").read_text())
spec = c["world"]["wind"]["profile"]
o = Observer(c)
rows = []
receipts = []
zone = None
f = (ROOT / "flight-trajectory.jsonl").open("w", buffering=1)


def sample():
    s = o.snapshot()
    if s["sim_s"] is None or "x500_0" not in s["poses"]:
        return None
    r = dict(
        run_id=c["run_id"],
        world_sha256=c["world"]["world_sha256"],
        sim_s=s["sim_s"],
        vehicle=s["poses"]["x500_0"],
        wind_probe={k: s["poses"].get(k) for k in ["wind_witness", "wind_control"]},
    )
    rows.append(r)
    f.write(json.dumps(r) + "\n")
    return r


try:
    deadline = time.monotonic() + 30
    while sample() is None:
        if time.monotonic() > deadline:
            raise TimeoutError("No Gazebo observations")
        time.sleep(0.1)
    for point in c["smoke_points"]:
        msg = Pose()
        msg.name = "x500_0"
        msg.position.x, msg.position.y, msg.position.z = point
        msg.orientation.w = 1
        # set_pose is idempotent. On a lost transport response, inspect the
        # observed position before retrying within the fixed wall-clock bound.
        deadline = time.monotonic() + 10
        next_request = 0.0
        while True:
            r = sample()
            if r and sum((a - b) ** 2 for a, b in zip(r["vehicle"]["xyz"], point)) < 0.0001:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Marker move not observed")
            if time.monotonic() >= next_request:
                o.node.request("/world/default/set_pose", msg, Pose, Boolean, 2000)
                next_request = time.monotonic() + 0.5
            time.sleep(0.05)
        new, velocity = requested_wind(spec, r["vehicle"], r["sim_s"], zone)
        a = o.activate_wind(velocity)
        a.update(
            run_id=c["run_id"],
            world_sha256=c["world"]["world_sha256"],
            phase="transport-marker-only",
            sequence=len(receipts),
            previous_zone=zone,
            zone=new,
            observation_sim_s=r["sim_s"],
            vehicle=r["vehicle"],
        )
        receipts.append(a)
        (ROOT / "wind-transitions.json").write_text(json.dumps(receipts, indent=2) + "\n")
        assert a["confirmed"]
        zone = new
        if len(receipts) == 1:
            (ROOT / "wind-activation.json").write_text(json.dumps(a, indent=2) + "\n")
        deadline = time.monotonic() + 30
        while True:
            r = sample()
            if r["sim_s"] >= a["end_sim_s"] + 15:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Force observation window")
            time.sleep(0.25)
    (ROOT / "worker-result.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "flight_invoked": False,
                "px4_invoked": False,
                "teleported_marker": True,
                "transitions": len(receipts),
            }
        )
        + "\n"
    )
finally:
    f.close()
    o.close()

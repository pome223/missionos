"""Preflight sensor control in isolated Gazebo; never sends aircraft commands."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import time


@contextmanager
def subscriptions(node, specs):
    """Release even partially registered subscriptions while Python is alive.

    Do not wait for Node/interpreter finalization to detach Python callbacks.
    The caller still owns the Node and must destroy it before recording success.
    """
    registered = []
    try:
        for message_type, topic, callback in specs:
            if not node.subscribe(message_type, topic, callback):
                raise RuntimeError("positive-control subscriptions rejected")
            registered.append(topic)
        yield
    finally:
        failed = []
        for topic in reversed(registered):
            try:
                if not node.unsubscribe(topic):
                    failed.append(topic)
            except Exception:
                # Still attempt to release the other callback on cleanup failure.
                failed.append(topic)
        if failed or node.subscribed_topics():
            raise RuntimeError("positive-control subscriptions not released")


def service(endpoint, reqtype, request):
    r = subprocess.run(
        [
            "gz",
            "service",
            "-s",
            "/world/default/" + endpoint,
            "--reqtype",
            reqtype,
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "5000",
            "--req",
            request,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if "data: true" not in r.stdout:
        raise RuntimeError("positive-control service rejected: " + endpoint)


def observe_probe(building, node, contacts_type, pose_type):
    name = "headroom_contact_positive_control"
    observations = {"messages": 0, "probe_contacts": 0, "probe_pose_seen": False}
    last_pose_names = None
    pose_wall = 0

    def contacts(message):
        observations["messages"] += 1
        for c in message.contact:
            if name in c.collision1.name or name in c.collision2.name:
                observations["probe_contacts"] += 1

    def poses(message):
        nonlocal last_pose_names, pose_wall
        last_pose_names = {p.name for p in message.pose}
        pose_wall = time.monotonic()
        if name in last_pose_names:
            observations["probe_pose_seen"] = True

    topic = f"/world/default/model/{building['name']}/link/link/sensor/building_contact/contact"
    specs = [
        (contacts_type, topic, contacts),
        (pose_type, "/world/default/pose/info", poses),
    ]

    lo, hi = building["lower_enu_m"], building["upper_enu_m"]
    x, y = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    sdf = f"""<sdf version="1.9"><model name="{name}"><pose>{x} {y} {hi[2]+0.7} 0 0 0</pose>
<link name="link"><inertial><mass>0.1</mass><inertia><ixx>0.001</ixx><iyy>0.001</iyy><izz>0.001</izz></inertia></inertial>
<collision name="collision"><geometry><sphere><radius>0.2</radius></sphere></geometry></collision>
</link></model></sdf>"""
    with subscriptions(node, specs):
        spawned = False
        try:
            service("create", "gz.msgs.EntityFactory", "sdf: " + json.dumps(sdf))
            spawned = True
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline and not observations["probe_contacts"]:
                time.sleep(0.1)
            if (
                not observations["probe_pose_seen"]
                or not observations["probe_contacts"]
            ):
                raise RuntimeError("no positive building contact observed")
        finally:
            if spawned:
                service("remove", "gz.msgs.Entity", f'name: "{name}" type: MODEL')
                removed_after = time.monotonic()
                deadline = removed_after + 10
                while time.monotonic() < deadline:
                    if (
                        pose_wall > removed_after
                        and last_pose_names is not None
                        and name not in last_pose_names
                    ):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("positive-control removal not observed")
    return {
        **observations,
        "subscriptions_released": True,
        "probe_removed_observed": True,
        "building_sensor_positive_control": True,
        "aircraft_commands_sent": False,
        "model_invoked": False,
    }


def main():
    from gz.transport13 import Node
    from gz.msgs10.contacts_pb2 import Contacts
    from gz.msgs10.pose_v_pb2 import Pose_V

    root = Path("/session")
    receipt = root / "contact-positive-control.json"
    # A failed repeated diagnostic must not leave a previous success receipt.
    receipt.unlink(missing_ok=True)
    scene = json.loads((root / "scene.json").read_text())
    node = Node()
    try:
        result = observe_probe(scene["buildings"][0], node, Contacts, Pose_V)
    finally:
        # Finalize the Node before writing success, also on exceptional exits.
        # The host must additionally require this process's exit code to be zero.
        del node
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

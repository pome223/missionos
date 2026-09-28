"""Run the real WindEffects/publisher/observer boundary without GPU or PX4."""

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_wind import add_wind, verify_wind  # noqa: E402
from src.runtime.yokohama_scene import sphere  # noqa: E402
from src.runtime.yokohama_wind_profile import make_profile  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--approve-sitl", action="store_true")
    p.add_argument("--flight-config", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    if not a.approve_sitl:
        p.error("Explicit simulation approval required")
    out = a.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    c = json.loads(a.flight_config.read_text())
    w = c["world"]
    w.pop("payload_delivery", None)
    w.pop("wind", None)
    w["probes"] = []
    sdf = ET.Element("sdf", version="1.9")
    node = ET.SubElement(sdf, "world", name="default")
    physics = ET.SubElement(node, "physics", name="1ms", type="ignored")
    ET.SubElement(physics, "max_step_size").text = ".004"
    ET.SubElement(physics, "real_time_factor").text = "1"
    for lib, cls in [
        ("physics", "Physics"),
        ("user-commands", "UserCommands"),
        ("scene-broadcaster", "SceneBroadcaster"),
    ]:
        ET.SubElement(
            node, "plugin", filename="gz-sim-" + lib + "-system", name="gz::sim::systems::" + cls
        )
    ship = w["sea_extension"]["ship_hold_world_xyz_m"]
    sphere(node, "x500_0", ship, False)
    ET.SubElement(node.find("model[@name='x500_0']"), "static").text = "true"
    worldpath = out / "models/worlds/default.sdf"
    worldpath.parent.mkdir(parents=True)
    ET.ElementTree(sdf).write(worldpath)
    m = out / "models/x500_base/model.sdf"
    m.parent.mkdir(parents=True)
    m.write_text('<sdf><model name="x500_base"><link name="base_link"/></model></sdf>')
    w = add_wind(out, w, 6, after_takeoff=True)
    w["wind"]["profile"] = make_profile(w, "harbor-nominal")
    entry = w["points"][0]["world_xyz_m"]
    direction = w["wind"]["profile"]["outward_unit_xy"]
    points = [
        ship,
        *[
            [entry[0] + s * direction[0], entry[1] + s * direction[1], 15]
            for s in [700, 300, 0, 300, 700, 1400]
        ],
    ]
    c = {
        "run_id": "wind-transport-" + uuid4().hex[:12],
        "world": w,
        "smoke_points": points,
        "claim": "teleported marker; no drone flight or AP",
        "wind_validation_scope": "transport-marker-only",
    }
    (out / "config.json").write_text(json.dumps(c, indent=2) + "\n")
    for rel in [
        "scripts/yokohama_sitl_worker.py",
        "scripts/ship_urban_camera_worker.py",
        "src/runtime/yokohama_wind_profile.py",
    ]:
        shutil.copy2(REPO / rel, out / Path(rel).name)
    shutil.copy2(REPO / "scripts/yokohama_wind_transport_worker.py", out / "transport_worker.py")
    name = "missionos-" + c["run_id"]

    def run(args, **kw):
        return subprocess.run(args, check=True, **kw)

    image = subprocess.run(
        ["docker", "image", "inspect", "px4io/px4-sitl-gazebo:latest", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--entrypoint",
                "sh",
                "-v",
                str(out) + ":/mission",
                image,
                "-c",
                "gz sim -s -r /mission/models/worlds/default.sdf",
            ],
            stdout=subprocess.DEVNULL,
        )
        with (
            (out / "worker.stdout").open("w") as stdout,
            (out / "worker.stderr").open("w") as stderr,
        ):
            run(
                ["docker", "exec", name, "python3", "/mission/transport_worker.py"],
                stdout=stdout,
                stderr=stderr,
                timeout=240,
            )
        rows = [json.loads(x) for x in (out / "flight-trajectory.jsonl").read_text().splitlines()]
        result = verify_wind(out, c, rows)
        (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
        assert result["status"] == "passed" and result["profile"]["all_zones_exercised"]
        print(json.dumps({"status": result["status"], "profile": result["profile"]}))
        (out / "run-result.json").write_text(
            json.dumps(
                {
                    "run_id": c["run_id"],
                    "status": result["status"],
                    "image_id": image,
                    "flight_invoked": False,
                    "px4_invoked": False,
                    "gpu_requested": False,
                    "teleported_marker": True,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        (out / "gazebo.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=True)
        (out / "cleanup.json").write_text(
            json.dumps({"owned_container_removed": True, "container": name}) + "\n"
        )


if __name__ == "__main__":
    main()

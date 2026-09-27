#!/usr/bin/env python3
"""Render opt-in stationary RGBD training pairs in CPU Gazebo, without PX4."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_scene import build_world, camera  # noqa: E402
from src.runtime.yokohama_adaptation import site_plan, digest, verify_export, validate_fresh_sites  # noqa: E402


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--approve-simulation", action="store_true")
    p.add_argument("--plan-version", choices=["head-v1", "attention-v2"], default="head-v1")
    p.add_argument(
        "--numpy-wheel",
        type=Path,
        required=True,
        help="Pinned NumPy 2.2.6 cp312 manylinux aarch64 wheel for isolated recorder",
    )
    args = p.parse_args()
    if not args.approve_simulation:
        p.error("Explicit --approve-simulation required; no hardware support")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    name = "missionos-yokohama-adapt-" + uuid4().hex[:10]
    result = dict(status="failed", flight_invoked=False, gpu_requested=False, container=name)
    created = False
    try:
        wheel = args.numpy_wheel.resolve()
        if not wheel.name.startswith(
            "numpy-2.2.6-cp312-cp312-manylinux"
        ) or not wheel.name.endswith("aarch64.whl"):
            raise ValueError("Unexpected recorder dependency wheel")
        with zipfile.ZipFile(wheel) as archive:
            if any(Path(n).is_absolute() or ".." in Path(n).parts for n in archive.namelist()):
                raise ValueError("Unsafe dependency archive")
            archive.extractall(root / "deps")
        image = run(
            ["docker", "image", "inspect", "px4io/px4-sitl-gazebo:latest", "--format", "{{.Id}}"]
        ).stdout.strip()
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                "-v",
                str(root) + ":/mission",
                image,
                "-c",
                "mkdir -p /mission/models/worlds /mission/models/x500 /mission/models/x500_base; "
                "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /mission/models/worlds/; "
                "cp /opt/px4-gazebo/share/gz/models/x500/model.sdf /mission/models/x500/; "
                "cp /opt/px4-gazebo/share/gz/models/x500_base/model.sdf /mission/models/x500_base/",
            ]
        )
        world = build_world(root, REPO / "docs/examples/yokohama-urban-scene", "contacts")
        route = [x["world_xyz_m"] for x in world["points"]]
        sites = site_plan(route, args.plan_version)
        path = root / "models/worlds/default.sdf"
        tree = ET.parse(path)
        w = tree.getroot().find("world")
        for m in list(w.findall("model")):
            if m.get("name") in {"scene_camera", *world["probes"]}:
                w.remove(m)
        model = ET.SubElement(w, "model", name="adapt_camera")
        link = ET.SubElement(model, "link", name="link")
        ET.SubElement(link, "gravity").text = "false"
        camera(link, "rgbd", [0, 0, 0], [0, 0, 0], "/adapt", rgbd=True, rate_hz=4)
        pp = ET.SubElement(
            model,
            "plugin",
            filename="gz-sim-pose-publisher-system",
            name="gz::sim::systems::PosePublisher",
        )
        ET.SubElement(pp, "publish_model_pose").text = "true"
        ET.SubElement(pp, "use_pose_vector_msg").text = "true"
        ET.SubElement(pp, "update_frequency").text = "250"
        tree.write(path, encoding="utf-8", xml_declaration=True)
        plan = dict(
            schema_version="yokohama_adaptation_plan.v1",
            sites=sites,
            plan_version=args.plan_version,
            previous_inspected_sites=site_plan(route)
            if args.plan_version == "attention-v2"
            else [],
            previous_site_separation_m=validate_fresh_sites(sites, site_plan(route))
            if args.plan_version == "attention-v2"
            else None,
            scene_sha256=world["source_scene_sha256"]
            if "source_scene_sha256" in world
            else digest(REPO / "docs/examples/yokohama-urban-scene/scene.json"),
            world_sha256=digest(path),
            image=image,
            field_of_view_deg=60,
            recorder_numpy_wheel_sha256=digest(wheel),
            scope="stationary sensor rendering with teleported camera; no AP or flight",
            context_frames=16,
            source_rate_hz=4,
            time_semantics="one commanded static viewpoint transition; index=1, not physical seconds",
        )
        (root / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        for script in ["yokohama_adaptation_worker.py", "ship_urban_camera_worker.py"]:
            shutil.copy2(REPO / "scripts" / script, root / script)
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
                "6",
                "--memory",
                "6g",
                "--entrypoint",
                "sh",
                "-v",
                str(root) + ":/mission",
                "-e",
                "LIBGL_ALWAYS_SOFTWARE=1",
                "-e",
                "PYTHONPATH=/mission/deps",
                image,
                "-c",
                "exec gz sim -s -r --headless-rendering -v 3 /mission/models/worlds/default.sdf",
            ]
        )
        created = True
        with (root / "worker.stdout").open("w") as out, (root / "worker.stderr").open("w") as err:
            subprocess.run(
                ["docker", "exec", name, "python3", "-u", "/mission/yokohama_adaptation_worker.py"],
                stdout=out,
                stderr=err,
                check=True,
                timeout=1200,
            )
        manifest = json.loads((root / "manifest.json").read_text())
        result.update(verify_export(root, manifest))
        result["manifest_sha256"] = digest(root / "manifest.json")
    except Exception as exc:
        result["reason"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if created:
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, timeout=30
            )
            (root / "simulator.log").write_text(logs.stdout + logs.stderr)
            no_gpu = subprocess.run(
                ["docker", "exec", name, "sh", "-c", "test ! -e /dev/nvidia0"], timeout=15
            )
            result["nvidia_device_absent"] = no_gpu.returncode == 0
            result["cleanup"] = (
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, timeout=30
                ).returncode
                == 0
            )
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" and result.get("cleanup") else 1


if __name__ == "__main__":
    sys.exit(main())

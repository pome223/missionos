#!/usr/bin/env python3
"""Opt-in local CPU Gazebo image pair; no PX4, aircraft flight, GPU or model API."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.yokohama_local_preflight import image_id, prepare_models  # noqa: E402
from src.runtime.yokohama_scene import build_world, camera, sha256  # noqa: E402
from src.runtime.yokohama_visual_input import PROMPT, CONTRACT, FiniteObservation  # noqa: E402


def command(args, timeout=30, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def compare(root):
    from PIL import Image
    import numpy as np

    records = json.loads((root / "frames.json").read_text())
    outcomes = {}
    for label in ("open", "blocked"):
        frames = [r for r in records if r["evaluation_label"] == label]
        state = FiniteObservation(frames[0]["stamps_s"]["rgb"])
        results = []
        for row in frames:
            if state.closed:
                break
            payload = {
                "prompt": PROMPT,
                "images": {k: (root / f).read_bytes() for k, f in row["images"].items()},
            }
            stamps = row["stamps_s"]
            if max(stamps.values()) - min(stamps.values()) > 0.15:
                raise ValueError("Paired input is temporally detached")
            result = state.observe(min(stamps.values()), max(stamps.values()), payload)
            result["sensor_stamps_s"] = stamps
            results.append(result)
        outcomes[label] = results
    assert outcomes["open"][0]["raw_candidate_before_Rules"]["kind"] == "translation"
    assert (
        len(outcomes["blocked"]) == 3
        and outcomes["blocked"][-1]["outcome"] == "stop_without_authority"
    )
    opened, blocked = records[0], records[3]
    assert opened["camera_pose"] == blocked["camera_pose"] and opened["goal"] == blocked["goal"]
    images = []
    for row in (opened, blocked):
        with Image.open(root / row["images"]["rgb"]) as im:
            images.append(np.asarray(im).astype(float))
    rgb = images[1]
    red = (
        (rgb[:, :, 0] > 80)
        & (rgb[:, :, 0] > rgb[:, :, 1] * 1.6)
        & (rgb[:, :, 0] > rgb[:, :, 2] * 1.6)
    )
    assert red[90:270, 160:480].sum() > 1500 and np.mean(np.abs(images[0] - images[1])) > 1
    return dict(
        status="passed",
        source_contract=CONTRACT,
        same_pose_goal_prompt=True,
        static_barrier_visible=True,
        central_red_pixels=int(red[90:270, 160:480].sum()),
        image_mean_absolute_difference=float(np.mean(np.abs(images[0] - images[1]))),
        model_input_keys=["prompt", "images"],
        outcomes=outcomes,
        vla_inference_invoked=False,
        learned_visual_performance_proven=False,
        no_rules_or_dispatch=True,
        camera_role="fixed replica of onboard viewpoint; no aircraft",
        limitation="Actual rendered RGB; CPU color double only. No learned VLA or obstacle avoidance qualification.",
    )


def run(root, image):
    root.mkdir(mode=0o700)
    run_id = uuid4().hex
    container_id = None
    report = dict(
        status="failed",
        run_id=run_id,
        image_id=image,
        network="none",
        gpu=False,
        physical_execution=False,
        created_utc=datetime.now(timezone.utc).isoformat(),
    )
    try:
        prepare_models(root, image)
        world = build_world(
            root, REPO / "docs/examples/yokohama-urban-scene", "flight", camera_rate_hz=4
        )
        a, b = [p["world_xyz_m"] for p in world["points"][:2]]
        yaw = math.atan2(b[1] - a[1], b[0] - a[0])
        unit = [math.cos(yaw), math.sin(yaw)]
        goal = [a[0] + 8 * unit[0], a[1] + 8 * unit[1], a[2]]
        config = dict(
            camera_pose=[*a, 0, 0, yaw],
            goal_world_xyz_m=goal,
            barrier_xyz_m=[a[0] + 4.5 * unit[0], a[1] + 4.5 * unit[1], a[2]],
            barrier_quat_wz=[math.cos(yaw / 2), math.sin(yaw / 2)],
            world=world,
            prompt=PROMPT,
            aircraft_spawned=False,
        )
        sdf = root / "models/worlds/default.sdf"
        tree = ET.parse(sdf)
        node = tree.getroot().find("world")
        node.find("physics/real_time_factor").text = "0"
        node.find("physics/max_step_size").text = "0.001"
        for name in ("scene_camera",):
            model = node.find(f"model[@name='{name}']")
            if model is not None:
                node.remove(model)

        def model(name, pose):
            m = ET.SubElement(node, "model", name=name)
            ET.SubElement(m, "static").text = "true"
            ET.SubElement(m, "pose").text = " ".join(map(str, pose))
            return ET.SubElement(m, "link", name="link")

        link = model("pair_camera", config["camera_pose"])
        camera(link, "rgb", [0.25, 0, 0.1], [0, 0, 0], "/yokohama/pair/rgb", rate_hz=10)
        camera(link, "down", [0.25, 0, 0.1], [0, math.pi / 2, 0], "/yokohama/pair/down", rate_hz=10)
        barrier = model("pair_barrier", [1000, 1000, a[2], 0, 0, yaw])
        for kind in ("visual", "collision"):
            part = ET.SubElement(barrier, kind, name="panel")
            ET.SubElement(
                ET.SubElement(ET.SubElement(part, "geometry"), "box"), "size"
            ).text = ".4 3 3"
            if kind == "visual":
                material = ET.SubElement(part, "material")
                for term in ("ambient", "diffuse"):
                    ET.SubElement(material, term).text = ".95 .04 .03 1"
        tree.write(sdf, encoding="utf-8", xml_declaration=True)
        config["world_sdf_sha256"] = sha256(sdf)
        (root / "pair-config.json").write_text(json.dumps(config, indent=2) + "\n")
        for name in ("yokohama_visual_pair_worker.py", "ship_urban_camera_worker.py"):
            shutil.copy2(REPO / "scripts" / name, root / name)
        report["source_sha256"] = {
            name: sha256(REPO / name)
            for name in (
                "scripts/run_yokohama_visual_pair.py",
                "scripts/yokohama_visual_pair_worker.py",
                "src/runtime/yokohama_visual_input.py",
                "src/runtime/yokohama_scene.py",
            )
        }
        container_id = command(
            [
                "docker",
                "create",
                "--label",
                "missionos.owner=" + run_id,
                "--label",
                "missionos.scope=yokohama-visual-pair",
                "--network",
                "none",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--entrypoint",
                "python3",
                "-v",
                str(root) + ":/mission",
                "-e",
                "LIBGL_ALWAYS_SOFTWARE=1",
                "-e",
                "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
                image,
                "/mission/yokohama_visual_pair_worker.py",
            ]
        ).stdout.strip()
        report["container_id"] = container_id
        command(["docker", "start", container_id])
        with (root / "worker.stdout").open("w") as out, (root / "worker.stderr").open("w") as err:
            worker = subprocess.run(
                ["docker", "wait", container_id], stdout=out, stderr=err, timeout=180
            )
        assert worker.returncode == 0 and (root / "worker.stdout").read_text().strip() == "0"
        report["verification"] = compare(root)
        report["status"] = "passed"
    except Exception as exc:
        report["reason"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if container_id:
            command(["docker", "stop", "--time", "10", container_id], check=False)
            (root / "container-inspect.json").write_text(
                command(["docker", "inspect", container_id]).stdout
            )
            (root / "container.log").write_text(
                command(["docker", "logs", container_id], check=False).stdout
            )
            report["owned_container_stopped_retained_for_preservation"] = True
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approve-gazebo", action="store_true")
    p.add_argument("--local-image-id", required=True)
    p.add_argument("--output-dir", required=True, type=Path)
    args = p.parse_args()
    if not args.approve_gazebo:
        p.error("Explicit local CPU Gazebo opt-in is required")
    root = args.output_dir.absolute()
    if root.exists() or root.is_symlink():
        p.error("Preserve old output; use a new directory")
    result = run(root, image_id(args.local_image_id))
    print(
        json.dumps({k: v for k, v in result.items() if k in ("status", "reason", "container_id")})
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

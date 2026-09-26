#!/usr/bin/env python3
"""Load the frozen Yokohama scene into an isolated CPU-only Gazebo/PX4 container."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_scene import build_world, sha256  # noqa: E402


def command(args, timeout=60, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["contacts", "flight"], required=True)
    parser.add_argument("--approve-sitl", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if not args.approve_sitl:
        parser.error("Explicit --approve-sitl is required; no hardware execution is supported")
    root = args.output_dir.resolve()
    if root.exists():
        parser.error("Output directory must not exist; preserve previous attempts")
    root.mkdir(parents=True)
    run_id = "yokohama-" + uuid4().hex[:12]
    container = "missionos-" + run_id
    result = {
        "run_id": run_id,
        "status": "failed",
        "phase": args.phase,
        "physical_execution_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "gpu_requested": False,
        "container": container,
    }
    created = False
    worker = None
    try:
        image = command(
            ["docker", "image", "inspect", "px4io/px4-sitl-gazebo:latest", "--format", "{{.Id}}"]
        ).stdout.strip()
        command(
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
                "mkdir -p /mission/models/worlds; cp /opt/px4-gazebo/share/gz/worlds/default.sdf /mission/models/worlds/; "
                "for model in x500 x500_base; do mkdir -p /mission/models/$model; cp /opt/px4-gazebo/share/gz/models/$model/model.* /mission/models/$model/; "
                "if [ -d /opt/px4-gazebo/share/gz/models/$model/meshes ]; then ln -s /opt/px4-gazebo/share/gz/models/$model/meshes /mission/models/$model/meshes; fi; done",
            ]
        )
        world = build_world(root, REPO / "docs/examples/yokohama-urban-scene", args.phase)
        config = {
            "run_id": run_id,
            "phase": args.phase,
            "world": world,
            "timeout_s": args.timeout_seconds,
            "operator_approval": "explicit CLI --approve-sitl",
            "hold_duration_sim_s": 30,
            "hold_horizontal_tolerance_m": 1.0,
            "hold_vertical_tolerance_m": 0.6,
            "hold_max_speed_mps": 0.5,
            "airspeed_mps": 3.0,
            "wind_mps": 0.0,
        }
        if args.phase == "flight":
            from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script
            from pyproj import Geod
            import math

            geod = Geod(ellps="WGS84")
            lon, lat = world["frame"]["home_lon_lat"]
            stages = []
            points = world["points"]
            previous = points[0]["world_xyz_m"]
            route_order = [0, 1, 2, 3, 2, 1, 0]
            for index, point_index in enumerate(route_order):
                point = points[point_index]
                target = point["world_xyz_m"]
                name = f"{index:02d}-" + point["id"]
                count = max(1, math.ceil(math.dist(previous, target) / 20))
                items = []
                for step in range(1, count + 1):
                    xyz = [a + (b - a) * step / count for a, b in zip(previous, target)]
                    bearing = math.degrees(math.atan2(xyz[0], xyz[1]))
                    glon, glat, _ = geod.fwd(lon, lat, bearing, math.hypot(*xyz[:2]))
                    items.append(
                        dict(
                            seq=len(items),
                            command=22 if index == 0 else 16,
                            latitude_deg=glat,
                            longitude_deg=glon,
                            altitude_m=xyz[2],
                            current=int(step == 1),
                            frame=6,
                            param2=0.5,
                            param4=math.degrees(
                                math.atan2(target[0] - previous[0], target[1] - previous[1])
                            ),
                        )
                    )
                facing = points[route_order[min(index + 1, len(route_order) - 1)]]["world_xyz_m"]
                yaw = (
                    math.degrees(math.atan2(facing[0] - target[0], facing[1] - target[1]))
                    if index < len(route_order) - 1
                    else 0
                )
                items.append(dict(items[-1], seq=len(items), command=17, current=0, param4=yaw))
                (root / (name + "-upload.py")).write_text(
                    _inner_upload_script(items, reuse_mavlink_session=index > 0)
                )
                stages.append(dict(name=name, target_world_xyz_m=target, items=items))
                previous = target
            config["flight_stages"] = stages
        (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        sources = [
            REPO / "src/runtime/yokohama_scene.py",
            Path(__file__),
            REPO / "scripts/yokohama_sitl_worker.py",
            REPO / "scripts/ship_urban_camera_worker.py",
            REPO / "scripts/ship_onboard_entrypoint.sh",
        ]
        if args.phase == "flight":
            sources.append(REPO / "scripts/yokohama_flight_worker.py")
        (root / "sources").mkdir()
        for source in sources:
            shutil.copy2(source, root / source.name)
            shutil.copy2(source, root / "sources" / source.name)
        result.update(
            image_id=image,
            world=world,
            source_sha256={p.name: sha256(p) for p in sources},
            started_utc=datetime.now(timezone.utc).isoformat(),
        )
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "none",
            "--cpus",
            "6",
            "--memory",
            "6g",
            "--entrypoint",
            "/bin/sh",
            "-v",
            str(root) + ":/mission",
            "-e",
            "LIBGL_ALWAYS_SOFTWARE=1",
            "-e",
            "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
        ]
        if args.phase == "flight":
            lon, lat = world["frame"]["home_lon_lat"]
            for entry in [
                "PX4_GZ_MODELS=/mission/models",
                "PX4_GZ_WORLDS=/mission/models/worlds",
                "PX4_GZ_WORLD=default",
                "PX4_SIM_MODEL=gz_x500",
                "HEADLESS=1",
                "PX4_GZ_NO_FOLLOW=1",
                f"PX4_HOME_LAT={lat}",
                f"PX4_HOME_LON={lon}",
                "PX4_HOME_ALT=0",
                "PX4_GZ_MODEL_POSE=0,0,0.3,0,0,0",
            ]:
                argv.extend(["-e", entry])
            argv.extend([image, "/mission/ship_onboard_entrypoint.sh", "-d"])
        else:
            argv.extend(
                [
                    image,
                    "-c",
                    "exec gz sim -s --headless-rendering -v 3 /mission/models/worlds/default.sdf",
                ]
            )
        command(argv, timeout=90)
        created = True
        (root / "container-inspect.json").write_text(
            command(["docker", "inspect", container]).stdout
        )
        if args.phase == "flight":
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                logs = command(["docker", "logs", container]).stdout
                if (
                    "Startup script returned successfully" in logs
                    and "gz_bridge] world: default, model: x500_0" in logs
                ):
                    break
                time.sleep(1)
            else:
                raise TimeoutError("PX4/Gazebo startup not observed")
        with (root / "worker.stdout").open("w") as out, (root / "worker.stderr").open("w") as err:
            worker = subprocess.Popen(
                ["docker", "exec", container, "python3", "-u", "/mission/yokohama_sitl_worker.py"],
                stdout=out,
                stderr=err,
            )
            worker.wait(timeout=args.timeout_seconds)
        result["worker_exit_code"] = worker.returncode
        if (root / "worker-result.json").exists():
            result["observed"] = json.loads((root / "worker-result.json").read_text())
        result["status"] = (
            "passed"
            if worker.returncode == 0 and result.get("observed", {}).get("status") == "passed"
            else "failed"
        )
    except Exception as exc:
        result["reason"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if created:
            logs = command(["docker", "logs", container], check=False)
            (root / "simulator.stdout").write_text(logs.stdout)
            (root / "simulator.stderr").write_text(logs.stderr)
            proc = command(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "ps -eo pid,args; test ! -e /dev/nvidia0",
                ],
                check=False,
            )
            (root / "processes.txt").write_text(proc.stdout + proc.stderr)
            result["nvidia_device_absent"] = proc.returncode == 0
            result["cleanup"] = (
                command(["docker", "rm", "-f", container], check=False).returncode == 0
            )
        if worker and worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (root / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": result["status"],
                "output": str(root),
                "reason": result.get("reason"),
            }
        )
    )
    return 0 if result["status"] == "passed" and result.get("cleanup") else 1


if __name__ == "__main__":
    sys.exit(main())

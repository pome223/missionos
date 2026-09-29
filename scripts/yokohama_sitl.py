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
    parser.add_argument("--decision-backend", choices=["fixture", "native"])
    parser.add_argument("--native-service-config", type=Path)
    parser.add_argument(
        "--capture-paired-views",
        action="store_true",
        help="Record withheld outcome views during CPU fixture flight",
    )
    parser.add_argument(
        "--capture-motion-views",
        action="store_true",
        help="Record bounded moving RGBD on the fixed CPU AP route",
    )
    parser.add_argument("--wam-profile", choices=["legacy", "motion-v4"], default="legacy")
    parser.add_argument(
        "--fixture-cold-start",
        action="store_true",
        help="Exercise slow startup/inference with CPU fixture delays",
    )
    parser.add_argument(
        "--sea-round-trip",
        action="store_true",
        help="Add an authored 1 km offshore stationary ship and AP round trip",
    )
    parser.add_argument(
        "--deliver-payload",
        action="store_true",
        help="Attach 50 g simulated cargo; gate return on observed pad receipt",
    )
    parser.add_argument(
        "--occupied-pad",
        action="store_true",
        help="CPU-only: ask MissionOS to wait for a scripted lead aircraft to unload and leave",
    )
    parser.add_argument(
        "--pad-approach-decision",
        action="store_true",
        help=(
            "After the pose-Rules wait clears, add a D3 model step toward the pad "
            "(VLA proposes, WAM forecasts, Rules and fresh pad reconfirmation constrain)"
        ),
    )
    parser.add_argument(
        "--fault-lead-return-on-d3-wam",
        action="store_true",
        help="CPU fixture fault injection: the departed lead flies back when D3 WAM is requested",
    )
    parser.add_argument(
        "--pad-state-advisory",
        choices=["assist", "shadow"],
        help="Opt-in learned CPU forecasts from a fixed pad camera during the occupied-pad hold",
    )
    parser.add_argument(
        "--wind-east-mps",
        type=float,
        default=0,
        help="Opt-in uniform eastward WindEffects stress, 0..8 m/s",
    )
    parser.add_argument(
        "--wind-profile",
        choices=["harbor-nominal", "harbor-upper"],
        help="Position-triggered wind zones; requires sea fixture flight and --wind-after-takeoff",
    )
    parser.add_argument(
        "--wind-after-takeoff",
        action="store_true",
        help="Start wind after the measured SEA-TAKEOFF hold",
    )
    parser.add_argument(
        "--gust-seed",
        type=int,
        help="Opt-in reproducible gusts on a wind profile (CPU fixture only)",
    )
    parser.add_argument(
        "--recover-city-hold",
        action="store_true",
        help="CPU fixture: revoke an interrupted decision, re-hold and observe again (at most 3 attempts)",
    )
    parser.add_argument(
        "--recovery-radius-m",
        type=float,
        help="Explicit recovery-only radius, 1..3 m; requires mapped clearance",
    )
    args = parser.parse_args()
    if args.pad_state_advisory and not args.occupied_pad:
        parser.error("Pad-state advisory requires --occupied-pad")
    if args.occupied_pad and (
        not args.deliver_payload
        or (args.decision_backend and not args.pad_approach_decision)
        or args.wind_east_mps
        or args.wind_profile
    ):
        parser.error(
            "Occupied-pad trial requires cargo delivery, zero wind, and a model backend "
            "only through --pad-approach-decision"
        )
    if args.fault_lead_return_on_d3_wam and (
        not args.pad_approach_decision or args.decision_backend != "fixture"
    ):
        parser.error("Lead-return fault needs the fixture pad approach")
    if args.pad_approach_decision and (
        not args.occupied_pad
        or not args.decision_backend
        or args.pad_state_advisory
        or args.recover_city_hold
        or args.capture_paired_views
        or args.wam_profile != "motion-v4"
    ):
        parser.error(
            "Pad approach decision requires --occupied-pad, a decision backend, the motion-v4 "
            "WAM profile, and no advisory, hold recovery or paired capture"
        )
    if args.recovery_radius_m is not None and (
        not args.recover_city_hold or not 1 <= args.recovery_radius_m <= 3
    ):
        parser.error("Recovery radius requires --recover-city-hold and a finite value in [1, 3]")
    if args.recover_city_hold and (args.decision_backend != "fixture" or args.capture_paired_views):
        parser.error("Hold recovery currently requires fixture decisions without paired capture")
    if args.gust_seed is not None and (
        args.wind_profile != "harbor-nominal" or not 0 <= args.gust_seed <= 2**32 - 1
    ):
        parser.error("Gust seed requires harbor-nominal and must be in [0, 2**32-1]")
    if args.wind_profile and (
        args.wind_east_mps
        or not args.wind_after_takeoff
        or not args.sea_round_trip
        or args.decision_backend != "fixture"
    ):
        parser.error("Wind profile requires sea fixture flight, delayed onset and no uniform wind")
    if args.wind_after_takeoff and (
        not (args.wind_east_mps or args.wind_profile) or not args.sea_round_trip
    ):
        parser.error("Delayed wind requires nonzero wind and --sea-round-trip")
    import math

    if not math.isfinite(args.wind_east_mps) or not 0 <= args.wind_east_mps <= 8:
        parser.error("Wind must be finite and in [0, 8] m/s")
    if args.wind_east_mps and args.phase != "flight":
        parser.error("Wind stress currently requires flight")
    if args.deliver_payload and (not args.sea_round_trip or args.phase != "flight"):
        parser.error("Payload delivery requires --sea-round-trip --phase flight")
    if args.sea_round_trip and (
        args.phase != "flight" or args.capture_motion_views or args.capture_paired_views
    ):
        parser.error("Sea round trip requires flight without dataset capture")
    if not args.approve_sitl:
        parser.error("Explicit --approve-sitl is required; no hardware execution is supported")
    if args.decision_backend and args.phase != "flight":
        parser.error("City decisions require --phase flight")
    if bool(args.native_service_config) != (args.decision_backend == "native"):
        parser.error("Native decisions require exactly one explicit --native-service-config")
    if args.capture_paired_views and args.decision_backend != "fixture":
        parser.error("Paired capture currently requires the CPU fixture backend")
    if args.capture_motion_views and (args.phase != "flight" or args.decision_backend):
        parser.error("Motion capture requires the fixed flight route without a decision backend")
    if args.wam_profile != "legacy" and not args.decision_backend:
        parser.error("WAM profile requires explicit decisions")
    if args.fixture_cold_start and args.decision_backend != "fixture":
        parser.error("Cold-start fixture delays require --decision-backend fixture")
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
    decision_host = None
    payload_receiver = None
    pad_supervisor = None
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
        world = build_world(
            root,
            REPO / "docs/examples/yokohama-urban-scene",
            args.phase,
            camera_rate_hz=4 if args.decision_backend or args.capture_motion_views else 2,
        )
        from src.runtime.yokohama_sea import extend_world, flight_stops

        if args.sea_round_trip:
            world = extend_world(root, REPO / "docs/examples/yokohama-urban-scene", world)
        if args.deliver_payload:
            from src.runtime.yokohama_payload import extend_world as add_payload

            world = add_payload(root, REPO / "docs/examples/yokohama-urban-scene", world)
        if args.occupied_pad:
            from src.runtime.yokohama_pad_queue import extend_world as add_pad_queue

            world = add_pad_queue(root, REPO / "docs/examples/yokohama-urban-scene", world)
            if args.pad_approach_decision:
                # A reached model endpoint may become the pad hold only within the
                # same translation bound that constrains every model segment.
                world["pad_queue"].update(model_hold_max_offset_m=5.01, reconfirm_timeout_wall_s=30)
                if args.fault_lead_return_on_d3_wam:
                    world["pad_queue"]["fault_lead_return"] = "on_d3_wam_request"
        if args.pad_state_advisory:
            from scripts.yokohama_pad_advisory_host import add_camera

            model_bundle = REPO / "docs/examples/yokohama-pad-state"
            world = add_camera(root, world, model_bundle, args.pad_state_advisory)
            shutil.copytree(model_bundle / "model", root / "pad-state-model")
        if args.wind_profile:
            from src.runtime.yokohama_wind_profile import make_profile

            profile = make_profile(world, args.wind_profile, args.gust_seed)
            args.wind_east_mps = profile["speeds_mps"]["offshore"]
        if args.wind_east_mps:
            from src.runtime.yokohama_wind import add_wind

            world = add_wind(root, world, args.wind_east_mps, after_takeoff=args.wind_after_takeoff)
            if args.wind_profile:
                world["wind"]["profile"] = profile
                world["wind"]["gusts"] = profile["gusts"]
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
            "wind_mps": args.wind_east_mps,
        }
        if args.capture_motion_views:
            config["motion_capture"] = dict(
                phases=["01-D2", "02-D3", "03-DELIVERY"],
                camera_rate_hz=4,
                hold_tail_s=8,
                max_frames=512,
                max_compressed_bytes=200 * 1024**2,
                reserve_bytes=96 * 1024**2,
                fraction_ranges={
                    "01-D2": [0.10, 0.55],
                    "02-D3": [0.48, 0.94],
                    "03-DELIVERY": [0.20, 0.70],
                },
                phase_lines={
                    name: [world["points"][i]["world_xyz_m"], world["points"][i + 1]["world_xyz_m"]]
                    for i, name in enumerate(["01-D2", "02-D3", "03-DELIVERY"])
                },
                purpose="AP motion acquisition; no model control or training",
            )
        if args.decision_backend:
            config["decisions"] = dict(
                backend=args.decision_backend,
                wam_profile=args.wam_profile,
                capture_paired_views=args.capture_paired_views,
                points=["D1", "D2", "D3"] if args.pad_approach_decision else ["D1", "D2"],
                camera_rate_hz=4,
                inference_timeout_s=75,
                startup_timeout_s=300 if args.wam_profile == "motion-v4" else 180,
                fixture_delay_s={"start": 170, "vla": 10, "wam": 55}
                if args.fixture_cold_start
                else {},
                shutdown_timeout_s=65,
                hold_drift_m=0.5,
                hold_speed_mps=0.3,
                maximum_model_translation_m=5.01,
                model_target_error_m=0.25,
                model_altitude_error_m=0.15,
                rules_margin_m=2,
                failure_response="bounded_stop_of_owned_SITL_container",
                sea_leg_present=args.sea_round_trip,
                payload_release_present=args.deliver_payload,
            )
            if args.pad_approach_decision:
                from src.runtime.yokohama_native import PAD_APPROACH_PROFILE

                # Measure the proposal-size bound from the model's own observation;
                # hold drift during inference stays bounded separately (0.5 m).
                config["decisions"]["size_bound_origin"] = "proposal_observation"
                config["decisions"]["pad_approach"] = dict(
                    wait_authority="pose Rules and fixture MissionOS judge; VLA grammar has no hold",
                    wam_profile=PAD_APPROACH_PROFILE,
                    structure_fraction_min=0.9,
                    known_pixel_fraction_floor=0.3,
                    discriminability_controls=["mirrored", "uniform"],
                    corridor_lateral_max_m=1.0,
                    reconfirmations=[
                        "before_model_approach_authority",
                        "model_endpoint_before_delivery_connector",
                    ],
                )
            if args.recover_city_hold:
                config["decisions"]["hold_recovery"] = dict(
                    max_attempts=3,
                    stable_sim_s=5,
                    timeout_wall_s=90,
                    maximum_anchor_distance_m=args.recovery_radius_m or 1,
                    before_dispatch_only=True,
                )
        if args.decision_backend or args.capture_motion_views or args.occupied_pad:
            # This pinned simulator's magnetometer frame conversion is not a
            # qualified heading source. Initialize once from the authored
            # launch orientation, then use PX4 inertial/GNSS estimation.
            config["simulator_initial_heading_deg"] = 90.0
            config["simulator_heading_runtime"] = {
                "px4_git": "381149fb012762f5e38c4a7fdc1b905b28038970",
                "gz_sim_version": "8.11.0-1~noble",
                "source": "authored_spawn_yaw_zero_ENU_equals_90deg_NED",
                "magnetometer_mode": "initialization_only_EKF2_MAG_TYPE_6",
                "continuous_ground_truth_heading_feed": False,
                "hardware_applicable": False,
            }
        if args.phase == "flight":
            from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script
            from pyproj import Geod
            import math

            geod = Geod(ellps="WGS84")
            lon, lat = world["frame"]["home_lon_lat"]
            stages = []
            stops = flight_stops(world)
            previous = stops[0]["target_world_xyz_m"]
            for index, stop in enumerate(stops):
                target = stop["target_world_xyz_m"]
                name = stop["name"]
                count = (
                    1
                    if stop.get("direct_endpoint")
                    else max(1, math.ceil(math.dist(previous, target) / 20))
                )
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
                facing = stops[min(index + 1, len(stops) - 1)]["target_world_xyz_m"]
                yaw = (
                    math.degrees(math.atan2(facing[0] - target[0], facing[1] - target[1]))
                    if index < len(stops) - 1
                    else 0
                )
                items.append(dict(items[-1], seq=len(items), command=17, current=0, param4=yaw))
                (root / (name + "-upload.py")).write_text(
                    _inner_upload_script(items, reuse_mavlink_session=index > 0)
                )
                stages.append(
                    dict(
                        **stop,
                        items=items,
                        arrival_timeout_s=max(
                            150,
                            math.ceil(math.dist(previous, target) / stop["airspeed_mps"] * 3 + 60),
                        ),
                    )
                )
                previous = target
            config["flight_stages"] = stages
        if args.recover_city_hold:
            from src.runtime.yokohama_native import recovery_envelopes

            policy = config["decisions"]["hold_recovery"]
            policy["mapped_envelopes"] = recovery_envelopes(
                config,
                REPO / "docs/examples/yokohama-urban-scene",
                policy["maximum_anchor_distance_m"],
            )
        (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        sources = [
            REPO / "src/runtime/yokohama_scene.py",
            REPO / "src/runtime/yokohama_sea.py",
            REPO / "src/runtime/yokohama_wind.py",
            REPO / "src/runtime/yokohama_wind_profile.py",
            Path(__file__),
            REPO / "scripts/yokohama_sitl_worker.py",
            REPO / "scripts/ship_urban_camera_worker.py",
            REPO / "scripts/ship_onboard_entrypoint.sh",
        ]
        if args.phase == "flight":
            sources.append(REPO / "scripts/yokohama_flight_worker.py")
        if args.occupied_pad:
            sources.extend(
                [
                    REPO / "src/runtime/yokohama_pad_queue.py",
                    REPO / "src/runtime/yokohama_pad_advisory_contract.py",
                    REPO / "scripts/yokohama_pad_worker.py",
                    REPO / "scripts/yokohama_decision_worker.py",
                ]
            )
        if args.pad_state_advisory:
            sources.extend(
                REPO / p
                for p in [
                    "scripts/yokohama_pad_advisory_host.py",
                    "scripts/yokohama_pad_advisory_worker.py",
                    "scripts/yokohama_pad_state.py",
                    "scripts/ship_anwm.py",
                    "src/intelligence/mission_assurance_agent.py",
                ]
            )
        if args.deliver_payload:
            sources.extend(
                [
                    REPO / "src/runtime/yokohama_payload.py",
                    REPO / "scripts/verify_yokohama_payload.py",
                ]
            )
        if args.capture_motion_views:
            sources.extend(
                [
                    REPO / "scripts/yokohama_motion_recorder.py",
                    REPO / "scripts/yokohama_decision_worker.py",
                ]
            )
        if args.decision_backend:
            sources.extend(
                REPO / p
                for p in [
                    "scripts/yokohama_decision_worker.py",
                    "scripts/yokohama_decision_host.py",
                    "src/runtime/yokohama_native.py",
                    "scripts/ship_anwm.py",
                    "scripts/yokohama_appearance.py",
                    "scripts/yokohama_wam_profile.py",
                    "scripts/ship_anwm_server.py",
                    "scripts/ship_aerovla.py",
                    "scripts/ship_aerovla_server.py",
                    "src/runtime/ship_vla_adapter.py",
                    "scripts/smoke_px4_gazebo_sitl_mission_upload.py",
                    "scripts/verify_yokohama_decisions.py",
                    "scripts/verify_yokohama_sitl.py",
                ]
            )
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
        if args.decision_backend:
            from scripts.yokohama_decision_host import DecisionHost

            service = (
                json.loads(args.native_service_config.read_text())
                if args.native_service_config
                else None
            )
            decision_host = DecisionHost(
                root,
                config,
                REPO / "docs/examples/yokohama-urban-scene",
                args.decision_backend,
                service,
            )
        if args.deliver_payload:
            from src.runtime.yokohama_payload import PayloadReceiver

            payload_receiver = PayloadReceiver(root, config)
        if args.occupied_pad:
            from src.runtime.yokohama_pad_queue import PadSupervisor

            pad_supervisor = PadSupervisor(root, config)
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
                "PX4_GZ_MODEL_POSE="
                + ",".join(
                    map(
                        str,
                        [
                            *world.get("sea_extension", {}).get("ship_deck_world_xyz_m", [0, 0, 0])[
                                :2
                            ],
                            0.3,
                            0,
                            0,
                            0,
                        ],
                    )
                ),
            ]:
                argv.extend(["-e", entry])
            if "simulator_initial_heading_deg" in config:
                argv.extend(["-e", "PX4_PARAM_EKF2_MAG_TYPE=6"])
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
        if pad_supervisor:
            try:
                pad_supervisor.close()
                result["pad_supervisor_stopped"] = True
                if pad_supervisor.advisory:
                    result["cpu_pad_state_inference_calls"] = pad_supervisor.advisory.calls
                    result["cpu_pad_state_released"] = pad_supervisor.advisory.closed
            except Exception as exc:
                result["pad_supervisor_error"] = str(exc)
                result["status"] = "failed"
        if payload_receiver:
            try:
                payload_receiver.close()
                result["payload_receiver_stopped"] = True
            except Exception as exc:
                result["payload_receiver_error"] = str(exc)
                result["status"] = "failed"
        if decision_host:
            try:
                result["model_shutdown"] = decision_host.close()
            except Exception as exc:
                result["model_shutdown_error"] = str(exc)
                result["status"] = "failed"
            result["decision_backend"] = args.decision_backend
            responses = list((root / "decisions").glob("*/native-response.json"))
            native_rows = [json.loads(p.read_text()) for p in responses]
            result["vla_invoked"] = any(r.get("vla_inference_invoked") is True for r in native_rows)
            result["wam_invoked"] = any(r.get("wam_inference_invoked") is True for r in native_rows)
            result["gpu_requested"] = args.decision_backend == "native"
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

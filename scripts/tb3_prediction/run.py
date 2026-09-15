"""Run fresh-container paired Nav2 development episodes; no cloud resources."""

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


from obstacle_geometry import SHAPES

ROOT = Path(__file__).resolve().parents[2]


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-sim", action="store_true", help="Opt in to local Gazebo/Nav2 execution"
    )
    parser.add_argument("--image", default="missionos-tb3-prediction:garden")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["clearing", "lingering"],
        default=["clearing", "lingering"],
    )
    parser.add_argument(
        "--policies",
        nargs="+",
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
        default=["nav2", "constant_velocity"],
    )
    parser.add_argument("--comparison-protocol", type=Path)
    parser.add_argument("--nwm-python", type=Path)
    parser.add_argument("--nwm-bundle", type=Path)
    parser.add_argument("--hold-manifest", type=Path)
    parser.add_argument("--agent-python", type=Path)
    parser.add_argument("--agent-env-file", type=Path)
    parser.add_argument("--approve-bounded-wait", action="store_true")
    parser.add_argument("--approve-bounded-detour", action="store_true")
    parser.add_argument("--approve-observed-continue", action="store_true")
    parser.add_argument("--refresh-costmaps", action="store_true")
    parser.add_argument("--nav2-detour", action="store_true")
    parser.add_argument("--compact-detour", action="store_true")
    parser.add_argument("--obstacle-shape", choices=SHAPES, default="box")
    parser.add_argument("--actor-initial-y", type=float)
    parser.add_argument("--actor-speed", type=float)
    parser.add_argument("--capture-seconds", type=float, default=20)
    parser.add_argument("--test-agent-plan-delay-wall-s", type=float, default=0)
    parser.add_argument("--test-agent-first-plan-delay-wall-s", type=float, default=0)
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
    if not 0 <= args.test_agent_plan_delay_wall_s <= 10:
        parser.error("test Agent delay must be between 0 and 10 wall seconds")
    if args.test_agent_plan_delay_wall_s and args.policies != ["agent_nwm"]:
        parser.error("test Agent delay requires only agent_nwm")
    if "agent_nwm" in args.policies and not (args.agent_python and args.hold_manifest):
        parser.error("agent_nwm requires --agent-python and --hold-manifest")
    if "agent_nwm" in args.policies and not args.approve_bounded_wait:
        parser.error("agent_nwm requires explicit --approve-bounded-wait")
    if args.approve_bounded_detour and (
        args.policies != ["agent_nwm"] or not args.compact_detour or args.nav2_detour
    ):
        parser.error("bounded detour requires only agent_nwm and compact-detour")
    if args.approve_observed_continue and args.policies != ["agent_nwm"]:
        parser.error("observed continuation requires only agent_nwm")
    if not 0 <= args.test_agent_first_plan_delay_wall_s <= 10 or (
        args.test_agent_first_plan_delay_wall_s
        and (args.test_agent_plan_delay_wall_s or args.policies != ["agent_nwm"])
    ):
        parser.error("first-plan delay requires agent_nwm, 0..10 seconds and no repeated delay")
    owner = os.environ.get("MISSIONOS_NAVIGATION_RUN_ID")
    owner_args = ["--label", f"missionos.navigation-run={uuid.UUID(owner)}"] if owner else []
    actor_args = [
        "--via-heading",
        args.via_heading,
        "--capture-seconds",
        str(args.capture_seconds),
        "--controller-profile",
        args.controller_profile,
    ]
    if all(p == "image_history" for p in args.policies):
        actor_args += ["--context-period", "0.5"]
    if args.approve_bounded_detour:
        actor_args.append("--approve-bounded-detour")
    if args.refresh_costmaps:
        actor_args.append("--refresh-costmaps")
    if args.nav2_detour:
        actor_args.append("--nav2-detour")
    if args.compact_detour:
        actor_args.append("--compact-detour")
    for name in ("actor_initial_y", "actor_speed"):
        if getattr(args, name) is not None:
            actor_args += ["--" + name.replace("_", "-"), str(getattr(args, name))]
    if any(p in ("nwm", "image_only", "agent_nwm") for p in args.policies) and not (
        args.nwm_python and args.nwm_bundle
    ):
        parser.error("image policies require explicit local Python and bundle paths")
    if not args.run_sim:
        print(
            json.dumps(
                {
                    "status": "not_run",
                    "reason": "--run-sim required",
                    "simulator_invoked": False,
                    "learned_wam_invoked": False,
                    "vla_invoked": False,
                    "physical_execution_invoked": False,
                }
            )
        )
        return 0
    out = (args.output or ROOT / "output/tb3-prediction" / time.strftime("%Y%m%dT%H%M%S")).resolve()
    if not out.is_relative_to(ROOT / "output"):
        parser.error("output must be inside this checkout output/ directory")
    out.mkdir(parents=True, exist_ok=False)
    hold_predictor = None
    if args.hold_manifest and any(p != "image_history" for p in args.policies):
        from nwm_hold_predict import HoldPredictor
        from nwm_passability import PassabilityPredictor

        manifest_path = args.hold_manifest
        predictor_class = (
            PassabilityPredictor
            if json.loads(manifest_path.read_text()).get("head_kind")
            else HoldPredictor
        )

        hold_predictor = predictor_class(args.nwm_bundle, args.hold_manifest)
        actor_args += ["--context-period", str(getattr(hold_predictor, "context_period", 0.25))]
        (out / "predictor-startup.json").write_text(json.dumps(hold_predictor.provenance, indent=2))
    image_id = run(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], capture_output=True
    ).stdout.strip()
    source_hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "scripts/tb3_prediction").glob("*"))
        if p.is_file()
    }
    manifest = {
        "schema_version": "tb3_prediction_comparison.v1",
        "phase": "development_baseline",
        "image_id": image_id,
        "obstacle_shape": args.obstacle_shape,
        "source_sha256": source_hashes,
        "episodes": [],
        "learned_wam_invoked": False,
        "vla_invoked": False,
        "physical_execution_invoked": False,
        "model_comparison_complete": False,
        "model_arms": {
            "wam": "not_run_no_qualified_navigation_checkpoint",
            "vla": "not_run_no_qualified_navigation_checkpoint",
            "wam_vla": "not_run_no_qualified_navigation_checkpoint",
        },
    }
    if args.comparison_protocol:
        manifest["comparison_protocol_sha256"] = hashlib.sha256(
            args.comparison_protocol.read_bytes()
        ).hexdigest()
        manifest["phase"] = "development_prediction_value"
    if hold_predictor:
        manifest["phase"] = "learned_hold_supervisor_comparison"
        manifest["predictor"] = hold_predictor.provenance
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for scenario, policy in itertools.product(args.scenarios, args.policies):
        episode = out / f"{scenario}-{policy}"
        episode.mkdir()
        inside = "/work/missionos/" + str(episode.relative_to(ROOT))
        name = "missionos-tb3-prediction-" + uuid.uuid4().hex[:10]
        created = False
        agent = None
        record = {"scenario": scenario, "policy": policy, "output": str(episode.relative_to(out))}
        try:
            if policy == "agent_nwm":
                from agent_process import AgentProcess

                agent = AgentProcess(
                    args.agent_python,
                    episode,
                    args.agent_env_file,
                    args.approve_bounded_detour,
                    args.approve_observed_continue,
                )
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    *owner_args,
                    "--shm-size=1g",
                    "-e",
                    "ROS_DOMAIN_ID=73",
                    "-e",
                    "ROS_LOCALHOST_ONLY=1",
                    "-e",
                    f"GZ_PARTITION={name}",
                    "-e",
                    f"IGN_PARTITION={name}",
                    "-e",
                    "RUN_MISSIONOS_TB3_PREDICTION_SIM=1",
                    "-v",
                    f"{ROOT}:/work/missionos",
                    "-w",
                    "/work/missionos",
                    "--entrypoint",
                    "bash",
                    args.image,
                    "scripts/tb3_prediction/start.sh",
                    inside,
                    args.obstacle_shape,
                    args.controller_profile,
                ],
                capture_output=True,
                timeout=30,
            )
            created = True
            with (episode / "episode.log").open("w") as stream:
                completed = subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        name,
                        "bash",
                        "-lc",
                        "source /opt/ros/humble/setup.bash\n"
                        "source /opt/turtlebot3_ws/install/setup.bash\n"
                        'exec /usr/bin/python3 scripts/tb3_prediction/episode.py "$@"',
                        "episode",
                        "--output",
                        inside,
                        "--scenario",
                        scenario,
                        "--policy",
                        policy,
                        *actor_args,
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + 280
                handled = False
                agent_handled = set()
                while completed.poll() is None:
                    if agent is not None:
                        for request_file in sorted(episode.glob("agent-request-*.json")):
                            if request_file.name in agent_handled:
                                continue
                            packet = json.loads(request_file.read_text())
                            if packet["phase"] == "plan":
                                packet["request"]["context"] = [
                                    str(ROOT / Path(p).relative_to("/work/missionos"))
                                    for p in packet["request"]["context"]
                                ]
                                prediction_dir = episode / request_file.stem
                                prediction_dir.mkdir()
                                packet["prediction"] = hold_predictor.select(
                                    packet["request"], prediction_dir
                                )
                                packet["request"]["context_sha256"] = [
                                    hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                    for p in packet["request"]["context"]
                                ]
                            delay = args.test_agent_plan_delay_wall_s or (
                                args.test_agent_first_plan_delay_wall_s if not agent_handled else 0
                            )
                            if packet["phase"] == "plan" and delay:
                                # Opt-in queue-latency fault, never a paused simulator or model claim.
                                before = json.loads((episode / "live-observation.json").read_text())
                                started = time.monotonic()
                                time.sleep(delay)
                                after = json.loads((episode / "live-observation.json").read_text())
                                (episode / ("delay-" + request_file.name)).write_text(
                                    json.dumps(
                                        dict(
                                            phase="after_prediction_before_agent",
                                            requested_wall_s=delay,
                                            observed_wall_s=time.monotonic() - started,
                                            before=before,
                                            after=after,
                                            synthetic_queue_delay=True,
                                            model_latency_claimed=False,
                                        ),
                                        indent=2,
                                    )
                                )
                            response = agent.request(packet)
                            if packet["phase"] == "plan":
                                response["prediction"] = packet["prediction"]
                            response_file = episode / request_file.name.replace(
                                "request", "response"
                            )
                            temporary = response_file.with_suffix(".tmp")
                            temporary.write_text(json.dumps(response, indent=2))
                            temporary.replace(response_file)
                            agent_handled.add(request_file.name)
                    request_path = episode / "value-request.json"
                    if request_path.exists() and not handled:
                        request = json.loads(request_path.read_text())
                        request["context"] = [
                            str(ROOT / Path(p).relative_to("/work/missionos"))
                            for p in request["context"]
                        ]
                        host_request = episode / "value-host-request.json"
                        host_request.write_text(json.dumps(request, indent=2))
                        if hold_predictor or request["policy"] == "image_history":
                            from camera_history_policy import select_history

                            response = (
                                select_history(request, episode)
                                if request["policy"] == "image_history"
                                else hold_predictor.select(request, episode)
                            )
                            temporary = episode / "value-response.tmp"
                            temporary.write_text(json.dumps(response, indent=2))
                            temporary.rename(episode / "value-response.json")
                        else:
                            subprocess.run(
                                [
                                    str(args.nwm_python),
                                    "scripts/tb3_prediction/value_selector.py",
                                    "--request",
                                    str(host_request),
                                    "--bundle",
                                    str(args.nwm_bundle),
                                ],
                                stdout=stream,
                                stderr=subprocess.STDOUT,
                                check=True,
                                timeout=70,
                            )
                        handled = True
                    if time.monotonic() > deadline:
                        completed.kill()
                        completed.wait()
                        raise TimeoutError("episode wall deadline")
                    time.sleep(0.1)
            record["exit_code"] = completed.returncode
            if (episode / "result.json").exists():
                result = json.loads((episode / "result.json").read_text())
                for key in [
                    "status",
                    "error",
                    "navigation_completion_verified",
                    "elapsed_sim_s",
                    "observed_path_length_m",
                    "min_cart_center_distance_m",
                    "stationary_sim_s",
                    "cart_contact_event_count",
                    "decision",
                    "learned_wam_invoked",
                    "decision_observation_age_sim_s",
                    "decision_to_end_sim_s",
                    "elapsed_decision_to_end_wall_s",
                ]:
                    if key in result:
                        record[key] = result[key]
            else:
                record["status"] = "startup_failed_no_episode_result"
        except (
            subprocess.SubprocessError,
            OSError,
            TimeoutError,
            RuntimeError,
            ValueError,
        ) as error:
            record["status"] = "runner_error"
            record["error"] = str(error)
        finally:
            if agent is not None:
                agent.close()
            if created:
                with (episode / "container.log").open("w") as stream:
                    subprocess.run(
                        ["docker", "logs", name],
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=10,
                    )
                cleanup = subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, timeout=20
                )
                record["container_removed"] = cleanup.returncode == 0
            manifest["episodes"].append(record)
            manifest["learned_wam_invoked"] = any(
                e.get("learned_wam_invoked", False) for e in manifest["episodes"]
            )
            if any(p in args.policies for p in ("nwm", "agent_nwm")):
                manifest["model_arms"]["wam"] = (
                    "experimental_NWM_future_mask_hold_supervisor; evaluate observed outcomes"
                    if hold_predictor
                    else "experimental_one_shot_NWM_route_selector; navigation_adoption_unverified"
                )
            manifest["model_comparison_complete"] = all(
                any(e["policy"] == policy for e in manifest["episodes"]) for policy in args.policies
            )
            (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            print(json.dumps(record), flush=True)
    return (
        0
        if all(
            e.get("navigation_completion_verified") or e.get("status") == "capture_complete"
            for e in manifest["episodes"]
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())

"""Opt-in local predictive navigation, with observed completion and owned cleanup."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

import click


SCHEMA = "missionos_predictive_navigation.v1"
OWNER = "missionos.navigation-run"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def write_receipt(folder: Path, value: dict) -> None:
    temporary = folder / "run.tmp"
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(folder / "run.json")


def prepare(config_path: Path, backend: str, policy: str, scene: str, output: Path) -> dict:
    if backend != "tb3":
        raise ValueError("public predictive navigation supports TB3 only")
    config = read_object(config_path)
    allowed = {
        "repository",
        "python",
        "bundle",
        "tb3_manifest",
        "agent_python",
        "agent_env_file",
        "tb3_actor_speed_mps",
        "tb3_actor_initial_y_m",
        "tb3_controller_profile",
        "tb3_via_heading",
        "test_agent_plan_delay_wall_s",
        "test_agent_first_plan_delay_wall_s",
    }
    if set(config) - allowed:
        raise ValueError("unknown configuration fields")

    def asset(key: str, directory: bool = False) -> Path:
        raw = config_path.parent / config[key]
        p = Path(os.path.abspath(raw)) if key in ("python", "agent_python") else raw.resolve()
        if not (p.is_dir() if directory else p.is_file()):
            raise ValueError(f"missing {key}")
        return p

    repo = asset("repository", directory=True)
    python = asset("python")
    if not os.access(python, os.X_OK):
        raise ValueError("configured Python is not executable")
    output = output.resolve()
    if not output.is_relative_to(repo / "output") or output == repo / "output":
        raise ValueError("run output must be a new directory inside repository output/")
    if output.exists():
        raise ValueError("run output already exists; use navigation status or a new directory")
    script = repo / "scripts/tb3_prediction/run.py"
    if not script.is_file():
        raise ValueError("predictive navigation runtime missing from configured repository")
    speed, initial = 0.29, -0.88
    if scene == "persistent":
        speed, initial = 0.04, -0.08
    if "tb3_actor_speed_mps" in config:
        speed = config["tb3_actor_speed_mps"]
        if (
            backend != "tb3"
            or scene != "clearing"
            or type(speed) not in (int, float)
            or not 0 <= speed <= 0.3
        ):
            raise ValueError("TB3 clearing actor speed must be between 0 and 0.3 m/s")
    if "tb3_actor_initial_y_m" in config:
        initial = config["tb3_actor_initial_y_m"]
        if (
            backend != "tb3"
            or scene != "clearing"
            or type(initial) not in (int, float)
            or not -1.2 <= initial <= -0.6
        ):
            raise ValueError("TB3 clearing actor initial y must be between -1.2 and -0.6 m")
    profile = config.get("tb3_controller_profile", "legacy")
    if profile != "legacy":
        raise ValueError("public controller profile must remain legacy")
    via_heading = config.get("tb3_via_heading", "outgoing")
    if via_heading not in ("outgoing", "incoming") or (
        "tb3_via_heading" in config and backend != "tb3"
    ):
        raise ValueError("via heading requires TB3 and outgoing or incoming")
    if via_heading == "incoming" and (scene != "clearing" or profile != "legacy"):
        raise ValueError("incoming via heading requires clearing scene and legacy controller")
    delay = config.get("test_agent_plan_delay_wall_s", 0)
    if type(delay) not in (int, float) or not 0 <= delay <= 10:
        raise ValueError("test Agent delay must be between 0 and 10 wall seconds")
    if delay and policy != "agent_nwm":
        raise ValueError("test Agent delay requires agent-nwm")
    first_delay = config.get("test_agent_first_plan_delay_wall_s", 0)
    if (
        type(first_delay) not in (int, float)
        or not 0 <= first_delay <= 10
        or (first_delay and (delay or policy != "agent_nwm"))
    ):
        raise ValueError("first-plan delay requires agent-nwm, 0..10 seconds and no repeated delay")
    command = [
        str(python),
        str(script),
        "--run-sim",
        "--output",
        str(output / "backend"),
        "--policies",
        policy,
        "--obstacle-shape",
        "wide_box",
        "--actor-speed",
        str(speed),
        "--actor-initial-y",
        str(initial),
    ]
    model_hash = None
    if policy in ("nwm", "agent_nwm"):
        bundle, manifest = asset("bundle"), asset(backend + "_manifest")
        model = read_object(manifest)
        if (
            model.get("status") != "complete"
            or model.get("head_kind") != "NWM_CDiT_future_obstacle_mask_hold_4s"
            or model.get("context_period_s") != 0.5
        ):
            raise ValueError("a completed 2 Hz four-second future-mask head is required")
        model_hash = digest(manifest)
        command += [
            "--nwm-python",
            str(python),
            "--nwm-bundle",
            str(bundle),
            "--hold-manifest",
            str(manifest),
        ]
    if policy == "agent_nwm":
        if backend != "tb3" or scene != "clearing":
            raise ValueError("Agent connection is limited to TB3 clearing scene")
        command += ["--agent-python", str(asset("agent_python")), "--approve-bounded-wait"]
        command += ["--test-agent-plan-delay-wall-s", str(delay)]
        command += ["--test-agent-first-plan-delay-wall-s", str(first_delay)]
        if config.get("agent_env_file"):
            command += ["--agent-env-file", str(asset("agent_env_file"))]
    scenario = "clearing" if backend == "tb3" else "blocked"
    command += ["--scenarios", scenario]
    if backend == "tb3":
        command += ["--controller-profile", profile, "--via-heading", via_heading]
        command += [
            "--image",
            "missionos-tb3-prediction:garden-repair",
            "--refresh-costmaps",
            "--compact-detour",
        ]
    return dict(
        schema_version=SCHEMA,
        run_id=str(uuid.uuid4()),
        status="not_run",
        scope="local_bounded_navigation_simulation",
        backend=backend,
        policy=policy,
        scene=scene,
        scenario=scenario,
        command=command,
        repository=str(repo),
        output=str(output),
        config_sha256=digest(config_path),
        cli_source_sha256=digest(Path(__file__)),
        runtime_source_sha256={p.name: digest(p) for p in script.parent.iterdir() if p.is_file()},
        agent_source_sha256={
            str(p.relative_to(repo)): digest(p)
            for p in [
                repo / name
                for name in (
                    "src/intelligence/navigation_agents.py",
                    "src/intelligence/navigation_prediction.py",
                    "src/runtime/tb3_predictive_recovery.py",
                    "src/intelligence/mission_assurance_agent.py",
                    "src/intelligence/missionos_mission_incident_graph.py",
                    "src/intelligence/mission_assurance_policy.py",
                )
            ]
        }
        if policy == "agent_nwm"
        else {},
        model_manifest_sha256=model_hash,
        runtime_invoked=False,
        simulator_invoked=False,
        physical_execution_invoked=False,
        navigation_completion_verified=False,
        learned_wam_invoked=False,
        landing_observed=False,
        evidence={},
    )


def cleanup_owned(run_id: str) -> bool:
    """Never act on a container without this invocation's exact UUID label."""
    uuid.UUID(run_id)
    found = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={OWNER}={run_id}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    for container in found.stdout.split():
        data = json.loads(
            subprocess.check_output(["docker", "inspect", container], timeout=10, text=True)
        )[0]
        if data["Config"].get("Labels", {}).get(OWNER) != run_id:
            raise ValueError("container ownership mismatch")
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=25, check=True
        )
    remaining = subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", f"label={OWNER}={run_id}"],
        timeout=10,
        text=True,
    )
    return not remaining.strip()


def collect(folder: Path, receipt: dict) -> None:
    manifest = folder / "backend/manifest.json"
    envelope = json.loads(manifest.read_text())
    episodes = envelope["episodes"] if isinstance(envelope, dict) else envelope
    if len(episodes) != 1 or episodes[0].get("policy") != receipt["policy"]:
        raise ValueError("unexpected runtime episode/policy")
    result = folder / "backend" / (receipt["scenario"] + "-" + receipt["policy"]) / "result.json"
    d = read_object(result)
    decision = d.get("decision", {})
    if d.get("policy") != receipt["policy"] or d.get("physical_execution_invoked") is not False:
        raise ValueError("runtime scope mismatch")
    if receipt["policy"] in ("nwm", "agent_nwm") and (
        d.get("learned_wam_invoked") is not True
        or (decision.get("provenance") or {}).get("task_manifest_sha256")
        != receipt["model_manifest_sha256"]
    ):
        raise ValueError("learned prediction provenance mismatch")
    if receipt["policy"] == "image_history" and d.get("learned_wam_invoked") is not False:
        raise ValueError("history comparison invoked a learned model")
    completed = d.get("status") == "completed" and d.get("navigation_completion_verified") is True
    if receipt["backend"] == "tb3":
        completed = (
            completed
            and d.get("goal_reached_observed") is True
            and (d.get("robot_motion_observed") is True and d.get("bridge_goals_succeeded", 0) > 0)
        )
    else:
        completed = completed and d.get("landing_observed") is True
    post = d.get("post_wait_verification")
    if decision.get("candidate") == "wait" and d.get("executed_candidate") in (
        "direct",
        "wait_then_direct",
    ):
        completed = completed and isinstance(post, dict) and post.get("passed") is True
    evidence_paths = [manifest, result]
    profile_path = result.parent / "controller-profile-readback.json"
    if profile_path.is_file():
        evidence_paths.append(profile_path)
    if "incoming" in receipt.get("command", []):
        profile = d.get("controller_profile_readback", {})
        completed = (
            completed
            and d.get("via_heading") == "incoming"
            and profile.get("profile") == "legacy"
            and profile.get("before_mission_start") is True
            and profile.get("parameters")
            == {
                "FollowPath.xy_goal_tolerance": 0.05,
                "general_goal_checker.xy_goal_tolerance": 0.25,
                "general_goal_checker.yaw_goal_tolerance": 0.25,
                "FollowPath.max_vel_x": 0.20,
                "FollowPath.RotateToGoal.lookahead_time": -1.0,
            }
        )
        completed = completed and profile_path.is_file() and profile == read_object(profile_path)
        stages = d.get("goal_stages", [])
        goal_count = d.get("bridge_goals_succeeded", 0)
        completed = completed and goal_count in (1, 2) and len(stages) == goal_count
        for i in range(goal_count if goal_count in (1, 2) else 0):
            path = result.parent / f"goal-{i}-request.json"
            completed = completed and path.is_file()
            if path.is_file() and i < len(stages):
                evidence_paths.append(path)
                request = read_object(path)
                payload = request.get("payload", {})
                xy = [0.35, -0.7] if goal_count == 2 and i == 0 else [1.0, 0.0]
                start = stages[i].get("robot_xy_m", [])
                if len(start) != 2 or any(
                    type(v) not in (float, int) or not math.isfinite(v) for v in start
                ):
                    completed = False
                    continue
                end = stages[i].get("end_robot_xy_m", [])
                if (
                    len(end) != 2
                    or any(type(v) not in (float, int) or not math.isfinite(v) for v in end)
                    or math.dist(end, xy) > 0.30
                ):
                    completed = False
                    continue
                outcome_path = result.parent / f"goal-{i}.json"
                completed = completed and outcome_path.is_file()
                if outcome_path.is_file():
                    evidence_paths.append(outcome_path)
                    completed = (
                        completed and read_object(outcome_path).get("nav2_status") == "succeeded"
                    )
                expected_yaw = (
                    math.atan2(xy[1] - start[1], xy[0] - start[0])
                    if goal_count == 2 and i == 0
                    else 0.0
                )
                completed = (
                    completed
                    and payload.get("x_m") == xy[0]
                    and payload.get("y_m") == xy[1]
                    and all(
                        type(v) in (int, float)
                        and math.isfinite(v)
                        and abs(v - expected_yaw) <= 1e-9
                        for v in (payload.get("yaw_rad"), stages[i].get("requested_yaw_rad"))
                    )
                    and request.get("physical_execution_invoked") is False
                )
    if receipt["policy"] == "agent_nwm":
        chain = result.parent / "agent-chain.json"
        events = read_object(chain)["events"]
        phases = [e["phase"] for e in events]
        if d.get("executed_candidate") == "observed_original_route_continue":
            response = d.get("continue_agent_response", {})
            verification = d.get("continue_verification", {})
            observed = d.get("pre_dispatch_observation", {})
            completed = (
                completed
                and receipt.get("observed_continue_approved") is True
                and d.get("agent_plan_response", {}).get("status") == "observe_original_route"
                and response.get("status") == "authorized_continue"
                and d.get("executed_wait_sim_s", 0) == 0
                and d.get("bridge_goals_succeeded") == 1
                and verification.get("passed") is True
                and type(verification.get("observed_exposure")) in (int, float)
                and 0 <= verification["observed_exposure"] <= 0.06
                and observed.get("robot_stationary") is True
                and type(observed.get("exposure")) in (int, float)
                and 0 <= observed["exposure"] <= 0.06
                and observed.get("sim_s", float("inf")) < response.get("dispatch_before_sim_s", 0)
                and d.get("agent_chain_completed_before_dispatch") is True
                and all(
                    p in phases
                    for p in (
                        "initial_assurance",
                        "continue_recovery",
                        "continue_assurance_graph",
                        "continue_rules",
                    )
                )
                and any(
                    e["phase"] == "continue_rules"
                    and e["result"].get("policy_authorized") is True
                    and e["result"].get("budget_reserved") is True
                    for e in events
                )
            )
            for name in ("continue-plan-before-agent.json", "continue-plan-before-dispatch.json"):
                planning = result.parent / name
                completed = completed and planning.is_file()
                if planning.is_file():
                    plan = read_object(planning)
                    completed = (
                        completed
                        and plan.get("status") == "succeeded"
                        and plan.get("waypoints") == [[1.0, 0.0]]
                    )
                    evidence_paths.append(planning)
            for name in ("goal-0-request.json", "goal-0.json"):
                goal_file = result.parent / name
                completed = completed and goal_file.is_file()
                if goal_file.is_file():
                    goal = read_object(goal_file)
                    if name.endswith("request.json"):
                        payload = goal.get("payload", {})
                        completed = completed and all(
                            payload.get(k) == v
                            for k, v in {
                                "x_m": 1.0,
                                "y_m": 0.0,
                                "yaw_rad": 0.0,
                                "max_speed_mps": 0.20,
                            }.items()
                        )
                    else:
                        completed = completed and goal.get("nav2_status") == "succeeded"
                    evidence_paths.append(goal_file)
            image = verification.get("image", {})
            path = result.parent / image.get("path", "missing")
            completed = completed and path.is_file() and digest(path) == image.get("sha256")
            if path.is_file():
                evidence_paths.append(path)
        else:
            completed = (
                completed
                and d.get("agent_chain_completed_before_dispatch") is True
                and 0 < d.get("executed_wait_sim_s", 0) <= 5.2
                and d.get("agent_plan_response", {}).get("status") == "authorized_wait"
                and (
                    d.get("post_wait_agent_response", {}).get("status") == "continue"
                    and "post_wait_assurance" in phases
                    if d.get("executed_candidate") == "wait_then_direct"
                    else receipt.get("bounded_detour_approved") is True
                    and d.get("executed_candidate") == "agent_detour_after_wait_observation"
                    and isinstance(post, dict)
                    and post.get("passed") is False
                    and type(post.get("observed_exposure")) in (int, float)
                    and 0.06 < post["observed_exposure"] <= 1
                    and "failed_wait_assurance" in phases
                    and d.get("detour_agent_response", {}).get("status") == "authorized_detour"
                    and d.get("bridge_goals_succeeded") == 2
                    and d.get("pre_dispatch_detour_plan", {}).get("status") == "succeeded"
                    and d.get("pre_dispatch_detour_plan", {}).get("waypoints")
                    == [[0.35, -0.7], [1.0, 0.0]]
                    and any(
                        e["phase"] == "detour_rules"
                        and e["result"].get("budget_reserved") is True
                        and e["result"].get("policy_authorized") is True
                        for e in events
                    )
                    and "detour_recovery" in phases
                    and "detour_assurance_graph" in phases
                )
                and all(p in phases for p in ("initial_assurance", "recovery", "rules"))
                and any(
                    e["phase"] == "rules"
                    and e["result"].get("policy_authorized") is True
                    and e["result"].get("budget_reserved") is True
                    for e in events
                )
            )
        evidence_paths.append(chain)
        if d.get("executed_candidate") == "agent_detour_after_wait_observation":
            for name in ("detour-plan-before-agent.json", "detour-plan-before-dispatch.json"):
                planning = result.parent / name
                completed = completed and planning.is_file()
                if planning.is_file():
                    evidence_paths.append(planning)
    receipt.update(
        simulator_invoked=True,
        navigation_completion_verified=bool(completed),
        learned_wam_invoked=d.get("learned_wam_invoked", False),
        landing_observed=d.get("landing_observed", False),
        decision=decision,
        executed_candidate=d.get("executed_candidate"),
        post_wait_verification=post,
        elapsed_sim_s=d.get("decision_to_end_sim_s", d.get("elapsed_sim_s")),
        contact_event_count=d.get("cart_contact_event_count", d.get("contact_event_count")),
        runner_container_removed=episodes[0].get("container_removed") is True,
        evidence={str(p.relative_to(folder)): digest(p) for p in evidence_paths},
    )


def execute(receipt: dict) -> dict:
    folder = Path(receipt["output"])
    folder.mkdir(parents=True, exist_ok=False)
    receipt.update(status="running", started_wall_epoch_s=time.time())
    write_receipt(folder, receipt)
    process = None
    try:
        env = dict(os.environ, MISSIONOS_NAVIGATION_RUN_ID=receipt["run_id"])
        with (folder / "runtime.log").open("w") as log:
            process = subprocess.Popen(
                receipt["command"],
                cwd=receipt["repository"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            receipt.update(runtime_invoked=True, simulator_invoked=None)
            receipt["runner_exit_code"] = process.wait(timeout=600)
        collect(folder, receipt)
        source_dir = Path(receipt["repository"]) / "scripts/tb3_prediction"
        if any(
            digest(source_dir / name) != expected
            for name, expected in receipt["runtime_source_sha256"].items()
        ):
            raise ValueError("runtime source changed during execution")
        if any(
            digest(Path(receipt["repository"]) / name) != expected
            for name, expected in receipt.get("agent_source_sha256", {}).items()
        ):
            raise ValueError("Agent source changed during execution")
        if digest(Path(__file__)) != receipt["cli_source_sha256"]:
            raise ValueError("CLI source changed during execution")
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(
            error=f"{type(error).__name__}: {error}", navigation_completion_verified=False
        )
    finally:
        try:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        except Exception as error:
            receipt.update(termination_error=str(error), navigation_completion_verified=False)
        try:
            receipt["cleanup_verified"] = cleanup_owned(receipt["run_id"])
        except Exception as error:
            receipt.update(cleanup_verified=False, cleanup_error=f"{type(error).__name__}: {error}")
        receipt["status"] = (
            "completed"
            if (
                receipt.get("runner_exit_code") == 0
                and receipt.get("navigation_completion_verified") is True
                and receipt.get("runner_container_removed") is True
                and receipt.get("cleanup_verified") is True
            )
            else "failed"
        )
        receipt["finished_wall_epoch_s"] = time.time()
        write_receipt(folder, receipt)
    return receipt


@click.group()
def navigation() -> None:
    """Run and inspect opt-in TB3 predictive navigation simulations."""


@navigation.command("run")
@click.argument("backend", type=click.Choice(["tb3"]))
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option(
    "--policy",
    type=click.Choice(["nwm", "image-history", "agent-nwm"]),
    default="nwm",
    show_default=True,
)
@click.option(
    "--scene", type=click.Choice(["clearing", "persistent"]), default="clearing", show_default=True
)
@click.option(
    "--approve-bounded-wait",
    is_flag=True,
    help="Approve one stationary wait of at most 5 seconds (4-second forecast plus at most 1 second for observed clearance progress); resume the original TB3 goal only after observed clearance and Assurance continuation.",
)
@click.option(
    "--approve-bounded-detour",
    is_flag=True,
    help="Additionally approve one fixed two-goal TB3 detour after observed wait failure, subject to Recovery, Assurance and Rules.",
)
@click.option(
    "--approve-observed-continue",
    is_flag=True,
    help="Separately approve one original-goal continuation after observed clearance, Agent judgment, Rules and fresh planning; no wait required.",
)
@click.option("--run-sim", is_flag=True, help="Authorize this bounded local simulator run.")
def run_command(
    backend: str,
    config_path: Path,
    output: Path,
    policy: str,
    scene: str,
    run_sim: bool,
    approve_bounded_wait: bool,
    approve_bounded_detour: bool,
    approve_observed_continue: bool,
) -> None:
    """Preview by default; --run-sim executes and verifies a fixed local route.

    These synthetic red-obstacle profiles use no Gateway or physical hardware.
    Asset configuration is local; no model download or training is performed.
    """
    try:
        if run_sim and policy == "agent-nwm" and not approve_bounded_wait:
            raise ValueError(
                "Agent execution requires --approve-bounded-wait for the fixed one-wait simulator policy"
            )
        receipt = prepare(config_path.resolve(), backend, policy.replace("-", "_"), scene, output)
        if approve_bounded_detour:
            if backend != "tb3" or policy != "agent-nwm" or not approve_bounded_wait:
                raise ValueError("bounded detour requires TB3 agent-nwm and bounded wait approval")
            receipt["command"].append("--approve-bounded-detour")
        if approve_observed_continue:
            if backend != "tb3" or policy != "agent-nwm" or not approve_bounded_wait:
                raise ValueError(
                    "observed continuation requires TB3 agent-nwm and bounded wait approval"
                )
            receipt["command"].append("--approve-observed-continue")
        receipt["observed_continue_approved"] = approve_observed_continue
        receipt["bounded_detour_approved"] = approve_bounded_detour
        if run_sim:
            click.echo(f"Run {receipt['run_id']}; log: {output / 'runtime.log'}", err=True)
            receipt = execute(receipt)
    except (KeyError, ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(receipt, indent=2))
    if receipt["status"] == "failed":
        raise click.exceptions.Exit(1)


@navigation.command("status")
@click.argument("output", type=click.Path(exists=True, file_okay=False, path_type=Path))
def status_command(output: Path) -> None:
    """Read a local run and check that its referenced runtime evidence is unchanged."""
    folder = output.resolve()
    try:
        receipt = read_object(folder / "run.json")
        if receipt.get("schema_version") != SCHEMA or Path(receipt["output"]).resolve() != folder:
            raise ValueError("incompatible run receipt")
        for relative, expected in receipt["evidence"].items():
            path = (folder / relative).resolve()
            if not path.is_relative_to(folder) or digest(path) != expected:
                raise ValueError("runtime evidence changed")
        if receipt["status"] == "completed":
            if not receipt["evidence"]:
                raise ValueError("completion evidence missing")
            collect(folder, receipt)
            if not (
                receipt["navigation_completion_verified"]
                and receipt["runner_container_removed"]
                and receipt.get("cleanup_verified") is True
                and receipt.get("runner_exit_code") == 0
            ):
                raise ValueError("completion claims do not match runtime evidence")
    except (KeyError, ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(receipt, indent=2))
    if receipt["status"] == "failed":
        raise click.exceptions.Exit(1)

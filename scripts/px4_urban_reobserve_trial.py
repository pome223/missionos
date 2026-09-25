#!/usr/bin/env python3
"""Opt-in PX4 stop/observe/resume paired development trial; CPU only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.px4_aerial_flight_session import (  # noqa: E402
    atomic_json,
    sign_command,
    STREAM_RATES,
    assert_container,
)
from scripts.px4_urban_wam_trial import (  # noqa: E402
    UrbanFlightSession,
    NAME,
    LABEL,
    OPT_IN,
    setup,
    call,
    wait_file,
    cleanup,
    require_opt_in,
)
from scripts.urban_navigation_contract import digest, route_check, segment_box_clearance  # noqa: E402
from scripts.urban_reobserve_contract import (  # noqa: E402
    IMAGE,
    MODES,
    BARRIER,
    PROTOCOL,
    routes,
    planner,
    truth_scene,
    validate,
)

EXTRA_SOURCES = (
    "scripts/urban_reobserve_contract.py",
    "scripts/px4_urban_reobserve_trial.py",
    "scripts/urban_checkpoint_capture.py",
)


def source_hashes():
    from scripts.px4_urban_headroom_trial import SOURCE_FILES

    names = (
        *SOURCE_FILES,
        *EXTRA_SOURCES,
        "scripts/prepare_px4_anwm_input.py",
        "scripts/urban_route_observer.py",
    )
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in names}


def barrier_sdf():
    return """<sdf version="1.9"><model name="reobserve_barrier"><static>true</static>
<pose>8 0 2.5 0 0 0</pose><link name="link">
<collision name="barrier_collision"><geometry><box><size>1 3 5</size></box></geometry></collision>
<visual name="barrier_visual"><geometry><box><size>1 3 5</size></box></geometry>
<material><ambient>0.95 0.25 0.05 1</ambient><diffuse>0.95 0.25 0.05 1</diffuse></material></visual>
<sensor name="building_contact" type="contact"><always_on>true</always_on><update_rate>50</update_rate>
<contact><collision>barrier_collision</collision></contact></sensor></link></model></sdf>"""


class ReobserveFlightSession(UrbanFlightSession):
    def __init__(self, directory):
        self.barrier_seen = None
        self.barrier_static = True
        super().__init__(directory)
        policy = {
            "protocol": PROTOCOL,
            "mode": self.config["mode"],
            "source_sha256": self.config["runtime_source_sha256"],
        }
        if (
            self.config["mode"] not in MODES
            or digest(policy) != self.config["reobserve_policy_sha256"]
        ):
            raise ValueError("reobservation policy binding differs")
        for name, expected in self.config["runtime_source_sha256"].items():
            if name.startswith("scripts/"):
                p = self.directory / name
                if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                    raise ValueError("controller source changed: " + name)
        self.contact_messages[BARRIER["name"]] = 0
        from gz.msgs10.contacts_pb2 import Contacts

        topic = "/world/default/model/reobserve_barrier/link/link/sensor/building_contact/contact"
        if not self.node.subscribe(Contacts, topic, lambda m: self.on_contact(BARRIER["name"], m)):
            raise RuntimeError("barrier contact subscription rejected")
        self.outcome.update(
            mode=self.config["mode"],
            selector="latest_depth_at_planned_stop",
            replan_count=0,
            stopped_at_checkpoint=False,
            safety_gate_rejected=False,
        )

    def on_pose(self, message):
        super().on_pose(message)
        found = [p for p in message.pose if p.name == BARRIER["name"]]
        if found:
            p = found[0]
            stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nsec
            if self.barrier_seen is None:
                self.barrier_seen = stamp
            self.barrier_static = math.dist(
                [p.position.x, p.position.y, p.position.z], BARRIER["translation_enu_m"]
            ) < 1e-5 and (
                max(
                    abs(p.orientation.x),
                    abs(p.orientation.y),
                    abs(p.orientation.z),
                    abs(p.orientation.w - 1),
                )
                < 1e-5
            )
        elif self.barrier_seen is not None:
            self.barrier_static = False

    def check_bounds(self, current):
        previous = self.previous_point or current["gazebo_pose_enu_m"]
        super().check_bounds(current)
        if self.barrier_seen is not None:
            clearance = segment_box_clearance(
                previous,
                current["gazebo_pose_enu_m"],
                BARRIER["lower_enu_m"],
                BARRIER["upper_enu_m"],
            )
            self.minimum_clearance = min(self.minimum_clearance, clearance)
            if not self.barrier_static or clearance < self.scene["required_clearance_m"]:
                raise RuntimeError("changed obstacle moved/disappeared or clearance breached")

    def consume(self, stage):
        path = self.directory / (stage + "-go.json")
        self.wait(lambda _: path.exists(), 180, stage + " decision")
        selection = json.loads((self.directory / (stage + "-selection.json")).read_text())
        envelope = json.loads(path.read_text())
        current = self.status()
        age = validate(envelope, self.config, self.key, selection, current, time.time(), stage)
        if stage == "resume" and (
            self.barrier_seen is None
            or not self.barrier_static
            or min(selection["input_frame_simulation_time_ns"]) <= self.barrier_seen
            or min(selection["input_frame_simulation_time_ns"]) <= self.checkpoint_stamp
        ):
            raise ValueError("post-stop and post-change observation required")
        # Both decisions are consumed once; later files cannot replay the stage.
        path.rename(self.directory / (stage + "-go-accepted.json"))
        self.event(
            "reobserve_decision_consumed",
            stage=stage,
            selection=selection,
            observation_age_wall_s=age,
            observed=current,
        )
        return selection, current

    def hold_for_command(self):
        initial, current = self.consume("approach")
        if initial["route_id"] != "forward":
            raise ValueError("unchanged initial depth must select forward for this case")
        self.event("reobserve_approach_dispatch", observed=current)
        self.execute_candidate(
            {"candidate_id": "approved_checkpoint", "target_local_ned_m": [0, 3, -3]}, current
        )
        stopped = self.stationary_at(
            [0, 3, -3], 45, position_tolerance=0.15, speed_limit=0.1, stable_sim_seconds=1.0
        )
        self.phase = "holding"
        self.checkpoint_stamp = stopped["pose_simulation_time_ns"]
        self.outcome["stopped_at_checkpoint"] = True
        self.event(
            "reobserve_checkpoint_stopped",
            observed=stopped,
            barrier_first_seen_sim_ns=self.barrier_seen,
        )
        atomic_json(
            self.directory / "checkpoint.json",
            {"observed": stopped, "barrier_first_seen_sim_ns": self.barrier_seen},
        )
        selected, current = self.consume("resume")
        route_id = selected["route_id"]
        if self.config["mode"] == "frozen_route" and route_id != initial["route_id"]:
            raise ValueError("frozen comparator changed route")
        route = routes("resume").get(route_id)
        check = (
            route_check(truth_scene(), route, start=current["gazebo_pose_enu_m"])
            if route
            else {"admissible": False}
        )
        self.event("reobserve_safety_gate", route_id=route_id, result=check, observed=current)
        if not check["admissible"]:
            self.outcome["safety_gate_rejected"] = True
            self.event("reobserve_safe_abort", observed=current)
            return
        self.route = route
        self.config.update(route_id=route_id, route_sha256=digest(route))
        self.outcome.update(route_id=route_id, replan_count=int(route_id != initial["route_id"]))
        self.execute_urban_route()

    def run(self):
        super().run()
        self.outcome.update(
            reobserve_verification_complete=True,
            safe_abort_observed=bool(
                self.outcome["safety_gate_rejected"]
                and self.outcome["landing_observed"]
                and self.outcome["disarm_observed"]
                and self.outcome["error"] is None
            ),
            barrier_first_seen_sim_ns=self.barrier_seen,
        )
        atomic_json(self.directory / "flight-result.json", self.outcome)
        return 0 if self.outcome["complete"] or self.outcome["safe_abort_observed"] else 1


def start_controller(root):
    session = root / "session"
    metadata = json.loads(call(["docker", "inspect", NAME]).stdout)[0]
    assert_container(metadata, session, name=NAME, label=LABEL)
    if (session / "events.jsonl").exists():
        raise ValueError("refusing second controller")
    call(
        [
            "docker",
            "exec",
            NAME,
            "/opt/px4-gazebo/bin/px4-mavlink",
            "start",
            "-u",
            "14600",
            "-o",
            "14650",
            "-t",
            "127.0.0.1",
            "-m",
            "onboard",
            "-r",
            "400000",
        ]
    )
    for stream, rate in STREAM_RATES.items():
        call(
            [
                "docker",
                "exec",
                NAME,
                "/opt/px4-gazebo/bin/px4-mavlink",
                "stream",
                "-u",
                "14600",
                "-s",
                stream,
                "-r",
                rate,
            ]
        )
    for filename, log in (
        ("urban_route_observer.py", "observer"),
        ("px4_urban_reobserve_trial.py --controller /session", "controller"),
    ):
        call(
            [
                "docker",
                "exec",
                "-d",
                "-e",
                OPT_IN + "=1",
                NAME,
                "sh",
                "-c",
                f"python3 /session/scripts/{filename} > /session/{log}.log 2>&1",
            ]
        )


def observe_and_dispatch(root, stage, progress=lambda *_: None):
    from scripts.select_urban_depth_route import depth_clouds, rank_routes
    from scripts.urban_checkpoint_capture import capture_program

    session = root / "session"
    history = session / ("history-" + stage)
    history.mkdir()
    for filename in ("airframe.sdf", "camera.sdf", "scene.json"):
        shutil.copyfile(session / filename, history / filename)
    scene = json.loads((session / "scene.json").read_text())
    for building in scene["buildings"]:
        filename = building["name"] + ".sdf"
        shutil.copyfile(session / filename, history / filename)
    config = json.loads((session / "config.json").read_text())
    atomic_json(
        history / "capture-config.json",
        {
            "frame_count": 16,
            "target_frame_interval_ns": 250000000,
            "target_tolerance_ns": 4000000,
            "require_contiguous_history": True,
            "capture_scope": "px4_sitl_airborne_observation",
            "scene_entity_names": [b["name"] for b in scene["buildings"]],
            "flight_session_status_file": "/session/status.json",
            "scene_geometry_sha256": scene["scene_sha256"],
            "decision_stage": stage,
            "reobserve_policy_sha256": config["reobserve_policy_sha256"],
            "authorized_capture_center_enu_m": [0, 0, 3] if stage == "approach" else [3, 0, 3],
        },
    )
    call(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "MISSIONOS_AERIAL_CAPTURE_ROOT=/session/history-" + stage,
            NAME,
            "python3",
            "-",
        ],
        input=capture_program(stage),
        timeout=150,
    )
    latest, _, receipt = depth_clouds(history)
    geometry = planner(stage, receipt["observation_enu_m"])
    choice = rank_routes(geometry, latest)
    selected = choice["route_id"]
    if stage == "resume" and config["mode"] == "frozen_route":
        selected = json.loads((session / "approach-selection.json").read_text())["route_id"]
    selection = {
        **receipt,
        "schema_version": "urban_reobserve_selection.v1",
        "stage": stage,
        **{k: config[k] for k in ("session_id", "scene_sha256", "mode")},
        "route_id": selected,
        "route_sha256": digest(routes(stage)[selected]) if selected else None,
        "latest_depth_choice": choice,
        "planner_input_sha256": digest(geometry),
        "model_invoked": False,
        "uses_scene_truth_for_selection": False,
    }
    atomic_json(session / (stage + "-planner-input.json"), geometry)
    atomic_json(session / (stage + "-selection.json"), selection)
    progress(stage + "_route_selected", {
        "stage": stage, "route_id": selected, "selection_sha256": digest(selection),
        "selection_is_not_execution": True, "model_invoked": False,
    })
    now = time.time()
    command = {
        k: config[k]
        for k in (
            "session_id",
            "scene_sha256",
            "approved_instruction_ref",
            "reobserve_policy_sha256",
        )
    }
    command.update(
        stage=stage,
        selection_sha256=digest(selection),
        issued_at_unix_s=now,
        expires_at_unix_s=now + PROTOCOL["dispatch_max_age_wall_s"],
    )
    atomic_json(
        session / (stage + "-go.json"),
        sign_command(command, (session / ".dispatch-key").read_bytes()),
    )
    print(
        json.dumps(
            {
                "mode": config["mode"],
                "stage": stage,
                "route_id": selected,
                "latest_depth_choice": choice["route_id"],
            }
        ),
        flush=True,
    )


def wait_event(root, name, *, stage=None, timeout=30):
    """Wait for an observed controller event, tolerating only a partial last line."""
    end = time.monotonic() + timeout
    path = root / "session/events.jsonl"
    while time.monotonic() < end:
        lines = path.read_text().splitlines(keepends=True) if path.exists() else []
        for line in lines:
            if not line.endswith("\n"):
                continue
            event = json.loads(line)
            if event["event"] == name and (stage is None or event.get("stage") == stage):
                return event
        time.sleep(0.1)
    raise TimeoutError("controller event not observed: " + name)


def run_case(args, mode):
    cohort = args.output_dir.resolve()
    frozen = json.loads((cohort / "protocol.json").read_text())
    if frozen["protocol"] != PROTOCOL or frozen["source_sha256"] != source_hashes():
        raise ValueError("runtime differs from frozen protocol")
    root = cohort / mode
    for previous in MODES[: MODES.index(mode)]:
        prior = cohort / previous
        if (
            not (prior / "session/flight-result.json").exists()
            or (prior / "host-error.json").exists()
        ):
            raise ValueError("previous case incomplete or failed; frozen pair stopped")
    if root.exists() or shutil.disk_usage(cohort).free < 350 * 1024**2:
        raise ValueError("fresh case and at least 350 MiB free required")
    run_session(root, args.assets_dir.resolve(), args.approved_instruction_ref, mode)


def run_session(root, assets, instruction, mode, progress=lambda *_: None, binding=None):
    """One source-bound session, shared by the paired trial and Gateway executor.

    The caller owns the shared simulator lock and authority admission. This
    function owns recovery and cleanup, including when a progress write fails.
    """
    require_opt_in()
    if mode not in MODES or not instruction.strip() or root.exists():
        raise ValueError("fresh session, declared mode and retained approval required")
    root.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root.parent).free < 512 * 1024**2:
        raise ValueError("at least 512 MiB free required")
    session = root / "session"
    try:
        setup(
            root, assets, "gap", "forward", instruction, IMAGE
        )
        atomic_json(root / "protocol.json", {
            "protocol": PROTOCOL, "source_sha256": source_hashes(),
            "scope": "single_session_execution_binding", "mode": mode,
        })
        if binding is not None:
            atomic_json(root / "gateway-binding.json", binding)
        config = json.loads((session / "config.json").read_text())
        policy = {"protocol": PROTOCOL, "mode": mode, "source_sha256": source_hashes()}
        config.update(
            mode=mode, runtime_source_sha256=source_hashes(), reobserve_policy_sha256=digest(policy)
        )
        atomic_json(session / "config.json", config)
        atomic_json(session / "reobserve-policy.json", policy)
        for filename in EXTRA_SOURCES:
            shutil.copyfile(ROOT / filename, session / filename)
        probe = call(
            ["docker", "exec", NAME, "python3", "/session/scripts/urban_headroom_contact_probe.py"],
            timeout=60,
        )
        atomic_json(root / "probe-process.json", {"returncode": probe.returncode})
        progress("contact_control_passed")
        start_controller(root)
        wait_file(session, "status.json", lambda s: s.get("phase") == "holding", 420)
        progress("observing_initial_depth")
        observe_and_dispatch(root, "approach", progress)
        current = wait_file(
            session,
            "status.json",
            lambda s: s.get("phase") == "flying_candidate" and s["gazebo_pose_enu_m"][0] >= 1,
            90,
        )
        sdf = barrier_sdf()
        (session / "barrier.sdf").write_text(sdf)
        r = call(
            [
                "docker",
                "exec",
                NAME,
                "gz",
                "service",
                "-s",
                "/world/default/create",
                "--reqtype",
                "gz.msgs.EntityFactory",
                "--reptype",
                "gz.msgs.Boolean",
                "--timeout",
                "5000",
                "--req",
                "sdf: " + json.dumps(sdf),
            ]
        )
        if "data: true" not in r.stdout:
            raise RuntimeError("barrier insertion rejected")
        atomic_json(
            root / "change-request.json",
            {
                "observed_before_request": current,
                "sdf_sha256": hashlib.sha256(sdf.encode()).hexdigest(),
                "request_accepted": True,
            },
        )
        checkpoint = wait_file(session, "checkpoint.json", lambda _: True, 90)
        if checkpoint["barrier_first_seen_sim_ns"] is None:
            raise RuntimeError("barrier not observed before stop")
        wait_file(session, "status.json", lambda s: s.get("phase") == "holding", 10)
        progress("checkpoint_stopped", {
            "checkpoint": checkpoint,
            "approach_consumed": wait_event(root, "reobserve_decision_consumed", stage="approach"),
        })
        progress("reobserving_depth")
        observe_and_dispatch(root, "resume", progress)
        consumed = wait_event(root, "reobserve_decision_consumed", stage="resume")
        gate = wait_event(root, "reobserve_safety_gate")
        progress("resume_authority_consumed", {"resume_consumed": consumed, "safety_gate": gate})
        if gate["result"]["admissible"]:
            progress("resume_dispatched", {"dispatch": wait_event(root, "urban_route_dispatch")})
        else:
            progress("safety_gate_rejected", {"safe_abort": wait_event(root, "reobserve_safe_abort")})
        result = wait_file(
            session,
            "flight-result.json",
            lambda s: s.get("reobserve_verification_complete") is True,
            600,
        )
        wait_file(session, "route-images/frames.json", lambda s: len(s["frames"]) > 0, 15)
        print(
            json.dumps(
                {
                    "mode": mode,
                    "complete": result["complete"],
                    "safe_abort": result["safe_abort_observed"],
                    "error": result["error"],
                }
            ),
            flush=True,
        )
        if result["error"] or not (result["complete"] or result["safe_abort_observed"]):
            raise RuntimeError("failed trial; stop frozen pair")
        progress("landing_observed", {
            "landing": wait_event(root, "landing_observed"),
            "disarm": wait_event(root, "disarm_observed"),
        })
    except Exception as exc:
        if root.exists():
            atomic_json(
                root / "host-error.json",
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "stderr": getattr(exc, "stderr", None),
                },
            )
        raise
    finally:
        if (root / "container.json").exists():
            if (session / "status.json").exists() and not (session / "flight-result.json").exists():
                try:
                    call(
                        [
                            "docker",
                            "exec",
                            NAME,
                            "pkill",
                            "-TERM",
                            "-f",
                            "^python3 /session/scripts/px4_urban_reobserve_trial.py",
                        ]
                    )
                    wait_file(session, "flight-result.json", lambda _: True, 120)
                except Exception as exc:
                    atomic_json(root / "recovery-error.json", {"error": str(exc)})
            cleanup(root)
            progress("simulator_removed")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--controller", type=Path)
    p.add_argument("--phase", choices=("freeze", "run"))
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--assets-dir", type=Path)
    p.add_argument("--approved-instruction-ref", default="")
    p.add_argument("--mode", choices=MODES)
    args = p.parse_args()
    if args.controller:
        require_opt_in()
        raise SystemExit(ReobserveFlightSession(args.controller).run())
    if args.output_dir is None or args.phase is None:
        p.error("phase and fresh output directory required")
    if args.phase == "freeze":
        args.output_dir.mkdir(parents=True)
        atomic_json(
            args.output_dir / "protocol.json",
            {
                "protocol": PROTOCOL,
                "source_sha256": source_hashes(),
                "frozen_at_unix_s": time.time(),
                "flights_at_freeze": 0,
            },
        )
        print(json.dumps({"frozen": True, "model_calls_permitted": 0}))
        return
    require_opt_in()
    if not args.assets_dir or not args.approved_instruction_ref.strip():
        p.error("assets and retained user approval reference required")
    from src.runtime.px4_depth_navigation import _exclusive

    # Same lock as the production depth executor, in addition to the unique container.
    with _exclusive("urban-depth-simulator"):
        for mode in [args.mode] if args.mode else MODES:
            run_case(args, mode)


if __name__ == "__main__":
    main()

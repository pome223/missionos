#!/usr/bin/env python3
"""Opt-in CPU-only headroom screen; never provisions or invokes an external model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.px4_urban_wam_trial import (  # noqa: E402
    UrbanFlightSession,
    setup,
    start,
    capture_history,
    wait_file,
    cleanup,
    call,
    NAME,
    validate_trigger,
    require_opt_in,
)
from scripts.px4_aerial_flight_session import (  # noqa: E402
    atomic_json,
    sign_command,
    wrap_angle,
)  # noqa: E402
from scripts.urban_headroom_contract import (  # noqa: E402
    CASES,
    PROTOCOL,
    case_scene,
    planner_geometry,
)  # noqa: E402
from scripts.urban_navigation_contract import digest, route_check  # noqa: E402

SOURCE_FILES = (
    "scripts/px4_urban_headroom_trial.py",
    "scripts/urban_headroom_contract.py",
    "scripts/select_urban_depth_route.py",
    "scripts/px4_urban_wam_trial.py",
    "scripts/urban_navigation_contract.py",
    "scripts/px4_aerial_flight_session.py",
    "scripts/probe_px4_aerial_camera.py",
    "src/prediction/px4_camera.py",
    "scripts/urban_headroom_contact_probe.py",
)


def source_hashes():
    return {
        n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCE_FILES
    }


def check_initial(current):
    if (
        math.dist(current["gazebo_pose_enu_m"], [0, 0, 3])
        > PROTOCOL["initial_position_tolerance_m"]
        or abs(wrap_angle(current["yaw_ned_rad"] - math.pi / 2))
        > PROTOCOL["initial_yaw_tolerance_rad"]
        or math.sqrt(sum(v * v for v in current["local_ned_velocity_mps"]))
        > PROTOCOL["initial_speed_limit_m_s"]
        or current["telemetry_age_seconds"] >= 2
    ):
        raise ValueError("frozen initial-state tolerance not met")


def validate_selection(selection, command, config, current, now):
    if (
        selection.get("schema_version") != "urban_depth_route_selection.v1"
        or selection.get("selector") != PROTOCOL["selector"]
        or selection.get("model_invoked") is not False
        or selection.get("model_forecast_used_for_dispatch") is not False
        or selection.get("uses_scene_truth_for_selection") is not False
        or selection.get("protocol_sha256") != digest(PROTOCOL)
        or any(selection.get(k) != config[k] for k in ("session_id", "scene_sha256"))
        or selection.get("route_id") != command.get("route_id")
        or selection.get("route_sha256") != command.get("route_sha256")
    ):
        raise ValueError("depth selection contract differs")
    age = now - selection["observation_unix_s"]
    if (
        not 0 <= age <= PROTOCOL["input_maximum_age_wall_s"]
        or math.dist(current["gazebo_pose_enu_m"], selection["observation_enu_m"])
        > PROTOCOL["observation_translation_drift_m"]
        or abs(
            wrap_angle(current["yaw_ned_rad"] - selection["observation_yaw_ned_rad"])
        )
        > PROTOCOL["observation_yaw_drift_rad"]
    ):
        raise ValueError("depth observation expired or moved")
    check_initial(current)
    return age


class HeadroomFlightSession(UrbanFlightSession):
    def hold_for_command(self):
        self.event("headroom_observation_ready", protocol_sha256=digest(PROTOCOL))
        self.wait(
            lambda _: (self.directory / "urban-go.json").exists(),
            300,
            "depth selection trigger",
        )
        envelope = json.loads((self.directory / "urban-go.json").read_text())
        raw = (self.directory / "depth-selection.json").read_bytes()
        selection = json.loads(raw)
        current = self.status()
        command = envelope["command"]
        if hashlib.sha256(raw).hexdigest() != command.get("selection_sha256"):
            raise ValueError("depth selection hash mismatch")
        age = validate_selection(selection, command, self.config, current, time.time())
        self.event(
            "headroom_choice_frozen",
            selection=selection,
            observed=current,
            observation_age_seconds=age,
        )
        try:
            validate_trigger(envelope, self.config, self.key, time.time())
            route = self.scene["routes"][command["route_id"]]
            if not route_check(self.scene, route, start=current["gazebo_pose_enu_m"])[
                "admissible"
            ]:
                raise ValueError("independent geometry rejects frozen choice")
        except (ValueError, KeyError):
            self.outcome["safety_filter_rejected_frozen_choice"] = True
            self.event("headroom_safety_rejection", route_id=command.get("route_id"))
            raise
        self.route = route
        self.config.update(
            route_id=command["route_id"], route_sha256=command["route_sha256"]
        )
        self.outcome.update(
            selector=PROTOCOL["selector"],
            route_id=command["route_id"],
            selection_sha256=command["selection_sha256"],
            safety_filter_rejected_frozen_choice=False,
        )
        (self.directory / "urban-go.json").rename(
            self.directory / "urban-go-accepted.json"
        )
        self.route_started_wall = time.monotonic()
        self.route_started_sim = current["pose_simulation_time_ns"]
        self.execute_urban_route()

    def check_bounds(self, current):
        super().check_bounds(current)
        if self.phase == "flying_candidate" and hasattr(self, "route_started_wall"):
            if (
                time.monotonic() - self.route_started_wall
                > PROTOCOL["route_deadline_wall_s"]
                or (current["pose_simulation_time_ns"] - self.route_started_sim) / 1e9
                > PROTOCOL["route_deadline_sim_s"]
            ):
                raise RuntimeError("frozen route deadline exceeded")


def freeze(root):
    if root.exists():
        raise ValueError("refusing to replace a frozen cohort")
    root.mkdir(parents=True)
    atomic_json(
        root / "protocol.json",
        {
            "protocol": PROTOCOL,
            "protocol_sha256": digest(PROTOCOL),
            "scenes": {case: case_scene(case) for case in CASES},
            "source_sha256": source_hashes(),
            "frozen_at_unix_s": time.time(),
            "cases_executed_at_freeze": 0,
        },
    )


def run_case(args, case):
    from scripts.select_urban_depth_route import select

    cohort = args.output_dir.resolve()
    frozen = json.loads((cohort / "protocol.json").read_text())
    if frozen["protocol"] != PROTOCOL or frozen["source_sha256"] != source_hashes():
        raise ValueError("protocol or implementation changed after freeze")
    root = cohort / case
    if root.exists():
        raise ValueError("case already attempted; no replacement or silent retry")
    session = root / "session"
    try:
        scene = case_scene(case)
        setup(
            root,
            args.assets_dir.resolve(),
            case,
            None,
            args.approved_instruction_ref,
            args.image,
            "headroom",
        )
        call(
            [
                "docker",
                "exec",
                NAME,
                "python3",
                "/session/scripts/urban_headroom_contact_probe.py",
            ],
            timeout=60,
        )
        print(json.dumps({"case": case, "scene_ready": True}), flush=True)
        start(root)
        initial = wait_file(
            session, "status.json", lambda s: s.get("phase") == "holding", 420
        )
        check_initial(initial)
        atomic_json(root / "initial-state.json", initial)
        capture_history(root)
        selection = select(root, planner_geometry(scene))
        config = json.loads((session / "config.json").read_text())
        route_id = selection["route_id"]
        selection.update(
            session_id=config["session_id"],
            scene_sha256=config["scene_sha256"],
            protocol_sha256=digest(PROTOCOL),
            route_sha256=digest(scene["routes"][route_id]) if route_id else None,
        )
        atomic_json(session / "depth-selection.json", selection)
        issued = time.time()
        command = {
            k: config[k]
            for k in ("session_id", "scene_sha256", "approved_instruction_ref")
        }
        command.update(
            route_id=route_id,
            route_sha256=selection["route_sha256"],
            selection_sha256=hashlib.sha256(
                (session / "depth-selection.json").read_bytes()
            ).hexdigest(),
            issued_at_unix_s=issued,
            expires_at_unix_s=issued + 4,
        )
        atomic_json(
            session / "urban-go.json",
            sign_command(command, (session / ".dispatch-key").read_bytes()),
        )
        print(
            json.dumps(
                {
                    "case": case,
                    "frozen_choices": {
                        k: v["route_id"] for k, v in selection["choices"].items()
                    },
                    "model_invoked": False,
                }
            ),
            flush=True,
        )
        outcome = wait_file(
            session,
            "flight-result.json",
            lambda s: s.get("urban_verification_complete") is True,
            600,
        )
        time.sleep(2)
        print(
            json.dumps(
                {
                    "case": case,
                    "complete": outcome["complete"],
                    "error": outcome["error"],
                }
            ),
            flush=True,
        )
        if not outcome["complete"]:
            raise RuntimeError("case did not complete; retain failure and stop cohort")
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
            if (session / "status.json").exists() and not (
                session / "flight-result.json"
            ).exists():
                try:
                    call(
                        [
                            "docker",
                            "exec",
                            NAME,
                            "pkill",
                            "-TERM",
                            "-f",
                            "^python3 /session/scripts/px4_urban_wam_trial.py",
                        ]
                    )
                    wait_file(session, "flight-result.json", lambda _: True, 120)
                except Exception as exc:
                    print(json.dumps({"landing_recovery_error": str(exc)}), flush=True)
            cleanup(root)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("freeze", "run"), required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--assets-dir", type=Path)
    p.add_argument("--approved-instruction-ref", default="")
    p.add_argument("--image", default="px4io/px4-sitl-gazebo:latest")
    p.add_argument("--case", choices=CASES)
    args = p.parse_args()
    if args.phase == "freeze":
        freeze(args.output_dir.resolve())
        print(
            json.dumps({"cohort_frozen": True, "cases": 12, "model_calls_permitted": 0})
        )
    else:
        require_opt_in()
        if args.assets_dir is None or not args.approved_instruction_ref.strip():
            p.error("asset directory and retained user instruction required")
        for case in ([args.case] if args.case else CASES):
            run_case(args, case)


if __name__ == "__main__":
    main()
